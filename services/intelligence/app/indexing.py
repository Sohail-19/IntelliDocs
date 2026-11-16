"""Stored parse -> chunks -> batched embeddings -> acknowledged vector persistence."""

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from intellidocs_intelligence.embeddings import (
    Embedder,
    Embedding,
    configured_embedder,
)
from intellidocs_intelligence.models import IndexResult, ParseResult

from .chunking import Chunker, ChunkingConfig
from .storage import Storage
from .vector_store import QdrantStore, VectorStore

IndexingFactory = Callable[[], AbstractContextManager["IndexingService"]]


def document_tree(result: ParseResult) -> Iterator[ParseResult]:
    yield result
    for attachment in result.attachments or []:
        yield from document_tree(attachment)


class IndexingService:
    def __init__(
        self,
        storage: Storage,
        chunker: Chunker,
        embedder: Embedder,
        store: VectorStore,
        batch_size: int,
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("EMBEDDING_BATCH_SIZE must be a positive integer")
        self.storage, self.chunker, self.embedder, self.store = storage, chunker, embedder, store
        self.batch_size = batch_size

    def index(self, doc_id: str) -> IndexResult:
        with self.storage.index_lock(doc_id):
            result = self.storage.load_result(doc_id)
            document = result.document
            if document.status != "ready":
                raise ValueError("Document must be parsed successfully before indexing")
            source = self.storage.source_path(doc_id, document.filename)
            content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            if document.content_hash is not None and content_hash != document.content_hash:
                raise ValueError("Stored source changed since parsing; ingest/parse it again")
            chunks = self.chunker.chunk(
                document,
                department=document.department,
                doc_type=document.doc_type,
                doc_date=document.doc_date,
            )
            basis = {
                "version": 1,
                "content_hash": content_hash,
                "embedding": self.embedder.cache_key(),
                "chunking": asdict(self.chunker.config),
                "chunks": [{"id": chunk.id, "text": chunk.text} for chunk in chunks],
            }
            fingerprint = hashlib.sha256(
                json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            cache = self.storage.load_index_cache(doc_id)
            vectors: list[Embedding] | None = None
            if cache and cache.get("fingerprint") == fingerprint:
                try:
                    vectors = TypeAdapter(list[Embedding]).validate_python(
                        cache.get("embeddings"), strict=True
                    )
                except ValidationError:
                    vectors = None
                if vectors is not None and len(vectors) != len(chunks):
                    vectors = None
            reused = vectors is not None
            if vectors is None:
                vectors = []
                for start in range(0, len(chunks), self.batch_size):
                    texts = [chunk.text for chunk in chunks[start : start + self.batch_size]]
                    batch = self.embedder.embed(texts)
                    if len(batch) != len(texts):
                        raise ValueError("Embedder returned the wrong batch size")
                    vectors.extend(batch)
            state: dict[str, Any] = {
                "fingerprint": fingerprint,
                "content_hash": content_hash,
                "embedding_model": self.embedder.model_name,
                "embeddings": vectors,
                "status": "prepared",
            }
            # Preserve computed vectors before Qdrant I/O so a failed write can retry
            # without loading/re-running the embedding model.
            self.storage.save_index_cache(doc_id, state)
            for start in range(0, len(chunks), self.batch_size):
                written = self.store.upsert_chunks(
                    chunks[start : start + self.batch_size],
                    embeddings=vectors[start : start + self.batch_size],
                )
                if written != len(chunks[start : start + self.batch_size]):
                    raise RuntimeError("Vector store did not acknowledge every chunk")
            # Upsert every new point before pruning obsolete boundaries; unrelated
            # documents and current chunks are never removed by this cleanup.
            self.store.delete_document(doc_id, keep_chunk_ids=[chunk.id for chunk in chunks])
            state.update(
                status="indexed", indexed_at=datetime.now(UTC).isoformat(), chunk_count=len(chunks)
            )
            self.storage.save_index_cache(doc_id, state)
            return IndexResult(
                doc_id=doc_id,
                chunk_count=len(chunks),
                embedding_model=self.embedder.model_name,
                warnings=["Reused unchanged-content embeddings."] if reused else [],
            )


@contextmanager
def indexer_from_env(storage: Storage) -> Iterator[IndexingService]:
    embedder = configured_embedder()
    settings = ChunkingConfig.from_env()
    if not os.environ.get("EMBEDDING_BATCH_SIZE"):
        raise ValueError("EMBEDDING_BATCH_SIZE is required")
    batch_size = int(os.environ["EMBEDDING_BATCH_SIZE"])
    store = QdrantStore.from_env(embedder)
    try:
        yield IndexingService(storage, Chunker(settings), embedder, store, batch_size)
    finally:
        store.client.close()
