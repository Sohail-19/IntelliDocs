"""Qdrant persistence for named dense/sparse vectors and full chunk payloads."""

import math
import os
from collections.abc import Sequence
from threading import RLock

from qdrant_client import QdrantClient, models

from intellidocs_intelligence.chunk_ids import chunk_id
from intellidocs_intelligence.embeddings import Embedder, Embedding
from intellidocs_intelligence.models import Chunk

from .base import VectorStore

PAYLOAD_INDEXES = {
    "department": models.PayloadSchemaType.KEYWORD,
    "doc_type": models.PayloadSchemaType.KEYWORD,
    "doc_date": models.PayloadSchemaType.DATETIME,
    "language": models.PayloadSchemaType.KEYWORD,
    "doc_id": models.PayloadSchemaType.KEYWORD,
}


class QdrantStore(VectorStore):
    def __init__(
        self,
        client: QdrantClient,
        collection_name: str,
        embedder: Embedder,
        *,
        dense_vector_name: str = "dense",
        sparse_vector_name: str = "sparse",
    ) -> None:
        if not all(
            value.strip() for value in (collection_name, dense_vector_name, sparse_vector_name)
        ):
            raise ValueError("Collection and vector names must not be empty")
        if dense_vector_name == sparse_vector_name:
            raise ValueError("Dense and sparse vector names must differ")
        self.client = client
        self.collection_name = collection_name
        self.embedder = embedder
        self.dense_vector_name = dense_vector_name
        self.sparse_vector_name = sparse_vector_name
        self._dimension: int | None = None
        self._lock = RLock()

    @classmethod
    def from_env(cls, embedder: Embedder) -> "QdrantStore":
        url = os.environ.get("QDRANT_URL", "")
        collection = os.environ.get("QDRANT_COLLECTION", "")
        if not url or not collection:
            raise ValueError("QDRANT_URL and QDRANT_COLLECTION are required")
        return cls(QdrantClient(url=url), collection, embedder)

    def _ensure_collection(self, dimension: int) -> None:
        modifier = (
            models.Modifier.IDF if self.embedder.sparse_requires_idf else models.Modifier.NONE
        )
        if self._dimension is not None:
            if dimension != self._dimension:
                raise ValueError("Embedding dimension does not match the initialized collection")
            return
        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    self.dense_vector_name: models.VectorParams(
                        size=dimension,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    self.sparse_vector_name: models.SparseVectorParams(modifier=modifier)
                },
            )
        info = self.client.get_collection(self.collection_name)
        vectors = info.config.params.vectors
        sparse = info.config.params.sparse_vectors or {}
        if (
            not isinstance(vectors, dict)
            or self.dense_vector_name not in vectors
            or vectors[self.dense_vector_name].size != dimension
            or vectors[self.dense_vector_name].distance != models.Distance.COSINE
            or self.sparse_vector_name not in sparse
        ):
            raise ValueError(
                "Existing collection has incompatible named vectors or dense dimension"
            )
        if (sparse[self.sparse_vector_name].modifier or models.Modifier.NONE) != modifier:
            raise ValueError(
                "Existing collection has incompatible sparse IDF modifier; "
                "use a new collection and re-index"
            )
        for field, schema in PAYLOAD_INDEXES.items():
            existing = info.payload_schema.get(field)
            if existing is not None:
                if existing.data_type != schema:
                    raise ValueError(f"Existing payload index {field!r} has an incompatible type")
            else:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field,
                    field_schema=schema,
                    wait=True,
                )
        self._dimension = dimension

    @staticmethod
    def _validate_vector(vector: Embedding, dimension: int) -> None:
        if len(vector["dense"]) != dimension or not all(math.isfinite(v) for v in vector["dense"]):
            raise ValueError("Dense embeddings must have a consistent size and finite values")
        indices, values = vector["sparse"]["indices"], vector["sparse"]["values"]
        if len(indices) != len(values) or len(set(indices)) != len(indices):
            raise ValueError("Sparse indices and values must align and indices must be unique")
        if any(type(index) is not int or not 0 <= index < 2**32 for index in indices):
            raise ValueError("Sparse indices must be unsigned 32-bit integers")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Sparse values must be finite")

    def upsert_chunks(
        self, chunks: Sequence[Chunk], embeddings: Sequence[Embedding] | None = None
    ) -> int:
        if not chunks:
            return 0
        identifiers = [chunk_id(chunk.doc_id, chunk.char_start, chunk.char_end) for chunk in chunks]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate document/offset identities in upsert batch")
        with self._lock:
            embeddings = (
                self.embedder.embed([chunk.text for chunk in chunks])
                if embeddings is None
                else embeddings
            )
            if len(embeddings) != len(chunks):
                raise ValueError("Embedder returned the wrong number of vectors")
            dimension = len(embeddings[0]["dense"])
            if dimension <= 0:
                raise ValueError("Dense embeddings must not be empty")
            for vector in embeddings:
                self._validate_vector(vector, dimension)
            points = []
            for identifier, chunk, vector in zip(identifiers, chunks, embeddings, strict=True):
                sparse = sorted(
                    zip(vector["sparse"]["indices"], vector["sparse"]["values"], strict=True)
                )
                points.append(
                    models.PointStruct(
                        id=identifier,
                        payload=chunk.model_dump(mode="json", exclude_none=True),
                        vector={
                            self.dense_vector_name: [float(value) for value in vector["dense"]],
                            self.sparse_vector_name: models.SparseVector(
                                indices=[index for index, _value in sparse],
                                values=[float(value) for _index, value in sparse],
                            ),
                        },
                    )
                )
            self._ensure_collection(dimension)
            self.client.upsert(collection_name=self.collection_name, points=points, wait=True)
        return len(points)

    def delete_document(self, doc_id: str, *, keep_chunk_ids: Sequence[str] = ()) -> None:
        if not doc_id:
            raise ValueError("A document ID is required")
        with self._lock:
            if not self.client.collection_exists(self.collection_name):
                return
            self.client.delete(
                collection_name=self.collection_name,
                wait=True,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="doc_id", match=models.MatchValue(value=doc_id)
                            ),
                        ],
                        must_not=(
                            [models.HasIdCondition(has_id=list(keep_chunk_ids))]
                            if keep_chunk_ids
                            else None
                        ),
                    )
                ),
            )
