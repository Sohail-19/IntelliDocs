"""Cold indexing benchmark for a folder of ParseResult JSON files (Python 3.11+)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import resource
import sys
import warnings
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.chunking import Chunker, ChunkingConfig
from app.indexing import document_tree
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import Embedder, Embedding, embedder_from_env
from intellidocs_intelligence.models import ParseResult
from qdrant_client import QdrantClient

ROOT = Path(__file__).resolve().parents[1]


def peak_rss_bytes() -> int:
    """OS high-water mark, including imports; Darwin uses bytes, Linux uses KiB."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


class MeasuredEmbedder(Embedder):
    def __init__(self, delegate: Embedder) -> None:
        self.delegate = delegate
        self.model_name = delegate.model_name
        self.load_seconds = 0.0
        self.embed_seconds = 0.0
        self.chunks = 0
        self.batches = 0
        self.dense_dimensions: int | None = None

    def load(self) -> None:
        start = perf_counter()
        self.delegate.load()
        self.load_seconds = perf_counter() - start

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        start = perf_counter()
        try:
            vectors = self.delegate.embed(texts)
        finally:
            self.embed_seconds += perf_counter() - start
        if len(vectors) != len(texts):
            raise ValueError("Embedder returned the wrong number of vectors")
        self.chunks += len(texts)
        self.batches += 1
        if vectors:
            self.dense_dimensions = len(vectors[0]["dense"])
        return vectors


def discover(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise ValueError(f"Parsed-document folder does not exist: {folder}")
    paths = sorted(path for path in folder.rglob("*.json") if path.name != "index_cache.json")
    if not paths:
        raise ValueError("No ParseResult JSON files found")
    return paths


def index_documents(
    paths: Sequence[Path],
    chunker: Chunker,
    embedder: MeasuredEmbedder,
    store: QdrantStore,
    batch_size: int,
) -> dict[str, Any]:
    """Use production components, always embedding: service disk caches are excluded."""
    if batch_size <= 0:
        raise ValueError("EMBEDDING_BATCH_SIZE must be a positive integer")
    documents: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    duplicate_documents = 0
    start = perf_counter()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Payload indexes have no effect")
        for path in paths:
            try:
                parsed = ParseResult.model_validate_json(path.read_text(encoding="utf-8"))
                for result in document_tree(parsed):
                    document = result.document
                    fingerprint = hashlib.sha256(
                        result.document.model_dump_json(exclude_none=True).encode()
                    ).hexdigest()
                    if document.id in seen:
                        if seen[document.id] != fingerprint:
                            raise ValueError(f"Conflicting ParseResults for doc_id {document.id}")
                        duplicate_documents += 1
                        continue
                    seen[document.id] = fingerprint
                    if document.status != "ready":
                        raise ValueError(f"Document {document.id} is not parsed successfully")
                    before = embedder.embed_seconds
                    chunks = chunker.chunk(
                        document,
                        department=document.department,
                        doc_type=document.doc_type,
                        doc_date=document.doc_date,
                    )
                    row: dict[str, Any] = {
                        "doc_id": document.id,
                        "input": str(path),
                        "chunks": len(chunks),
                        "indexed_chunks": 0,
                        "status": "indexing",
                    }
                    documents.append(row)
                    for offset in range(0, len(chunks), batch_size):
                        batch = chunks[offset : offset + batch_size]
                        vectors = embedder.embed([chunk.text for chunk in batch])
                        acknowledged = store.upsert_chunks(batch, embeddings=vectors)
                        if acknowledged != len(batch):
                            raise RuntimeError("Vector store did not acknowledge every chunk")
                        row["indexed_chunks"] += acknowledged
                    row.update(status="ok", embedding_seconds=embedder.embed_seconds - before)
            except Exception as error:
                # Keep a report and nonzero exit status even after partial writes.
                errors.append({"input": str(path), "error": str(error)})
                if documents and documents[-1]["status"] == "indexing":
                    documents[-1]["status"] = "error"
    return {
        "complete": not errors,
        "documents": sum(row["status"] == "ok" for row in documents),
        "chunks": sum(row["indexed_chunks"] for row in documents),
        "duplicate_documents_skipped": duplicate_documents,
        "indexing_seconds": perf_counter() - start,
        "per_document": documents,
        "errors": errors,
    }


def collection_size(
    client: QdrantClient, collection: str, local_path: Path | None
) -> dict[str, Any]:
    exists = client.collection_exists(collection)
    points = client.count(collection, exact=True).count if exists else 0
    if local_path is not None:
        # Closing flushes SQLite before measuring the dedicated collection directory.
        client.close()
        directory = local_path / "collection" / collection
        size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
        return {"points": points, "size_bytes": size, "size_method": "local_sqlite_files"}
    if not exists:
        return {"points": 0, "size_bytes": 0, "size_method": "server_snapshot"}
    snapshot = client.create_snapshot(collection_name=collection, wait=True)
    if snapshot is None:
        raise RuntimeError("Qdrant did not return collection snapshot size")
    try:
        return {"points": points, "size_bytes": snapshot.size, "size_method": "server_snapshot"}
    finally:
        client.delete_snapshot(collection_name=collection, snapshot_name=snapshot.name, wait=True)


def package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def summary(report: dict[str, Any]) -> str:
    embedding = report["embedding"]
    per_chunk = embedding["seconds_per_chunk"]
    rows = [
        ("Documents indexed", str(report["documents"])),
        ("Chunks acknowledged", str(report["chunks"])),
        ("Embedding ms/chunk", "n/a" if per_chunk is None else f"{per_chunk * 1000:.4f}"),
        ("Model load seconds", f"{embedding['model_load_seconds']:.6f}"),
        ("Peak process RSS MiB", f"{report['peak_rss_bytes'] / 1024**2:.2f}"),
        ("Qdrant points", str(report["qdrant"]["points"])),
        ("Qdrant size bytes", str(report["qdrant"]["size_bytes"])),
        ("Qdrant size method", report["qdrant"]["size_method"]),
    ]
    lines = [f"{'Metric':<28} Value", *(f"{name:<28} {value}" for name, value in rows)]
    if not report["complete"]:
        lines.append("INCOMPLETE: partial collection; see errors in JSON. Exit status 1.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--documents", type=Path, required=True, help="Only ParseResult JSON, recursively"
    )
    qdrant = cli.add_mutually_exclusive_group()
    qdrant.add_argument("--qdrant-path", type=Path, help="Persistent SDK local mode (default)")
    qdrant.add_argument(
        "--qdrant-url", help="Real server; creates a dedicated benchmark collection"
    )
    cli.add_argument("--collection", help="Must not already exist; retained after the run")
    cli.add_argument("--output", type=Path)
    cli.add_argument("--overwrite", action="store_true", help="Overwrite JSON report only")
    args = cli.parse_args(argv)
    now = datetime.now(UTC)
    run_id = now.strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
    output = args.output or ROOT / "eval/results" / f"index_{run_id}.json"
    collection = args.collection or f"index_bench_{run_id}"
    local_path = (
        None
        if args.qdrant_url
        else (args.qdrant_path or ROOT / "eval/results" / f"qdrant_{run_id}")
    )
    client: QdrantClient | None = None
    try:
        if output.exists() and not args.overwrite:
            raise ValueError(f"Result already exists: {output}; use --output or --overwrite")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", collection):
            raise ValueError("Collection name must contain only letters, digits, _ or -")
        paths = discover(args.documents)
        chunking = ChunkingConfig.from_env()
        batch_size = int(os.environ.get("EMBEDDING_BATCH_SIZE", "0"))
        if batch_size <= 0:
            raise ValueError("EMBEDDING_BATCH_SIZE must be a positive integer")
        manifest = [
            {
                "file": str(path.relative_to(args.documents)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in paths
        ]
        client = (
            QdrantClient(path=str(local_path))
            if local_path is not None
            else QdrantClient(url=args.qdrant_url, api_key=os.environ.get("QDRANT_API_KEY"))
        )
        if client.collection_exists(collection):
            raise ValueError("Benchmark collection already exists; choose a fresh name")
        embedder = MeasuredEmbedder(embedder_from_env())
        embedder.load()
        store = QdrantStore(client, collection, embedder)
        report = index_documents(paths, Chunker(chunking), embedder, store, batch_size)
        report.update(
            created_at=now.isoformat(),
            cold_embeddings=True,
            input_files=manifest,
            corpus_sha256=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            chunking=asdict(chunking),
            embedding={
                "identity": embedder.delegate.cache_key(),
                "batch_size": batch_size,
                "batches": embedder.batches,
                "embedded_chunks": embedder.chunks,
                "dense_dimensions": embedder.dense_dimensions,
                "seconds": embedder.embed_seconds,
                "seconds_per_chunk": embedder.embed_seconds / embedder.chunks
                if embedder.chunks
                else None,
                "model_load_seconds": embedder.load_seconds,
            },
            qdrant={
                "mode": "sdk_local" if local_path is not None else "server",
                "collection": collection,
                "local_path": str(local_path) if local_path is not None else None,
                **collection_size(client, collection, local_path),
            },
            peak_rss_bytes=peak_rss_bytes(),
            rss_scope="Lifetime high-water RSS of this Python process; excludes remote server/GPU",
            environment={
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "logical_cpus": os.cpu_count(),
                "packages": {
                    name: package_version(name)
                    for name in (
                        "qdrant-client",
                        "pydantic",
                        "torch",
                        "sentence-transformers",
                        "FlagEmbedding",
                    )
                },
            },
        )
        if report["qdrant"]["points"] != report["chunks"]:
            report["complete"] = False
            report["errors"].append(
                {"error": "Qdrant point count differs from acknowledged chunks"}
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if args.overwrite else "x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        print(summary(report))
        print(f"Results: {output}")
        return 0 if report["complete"] else 1
    except Exception as error:
        print(f"Index benchmark failed: {error}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
