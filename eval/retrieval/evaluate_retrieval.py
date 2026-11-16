"""Compare retrieval configurations against human-labeled page and clause targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from statistics import mean
from typing import TypedDict, TypeVar

from app.reranking import RerankerConfig, create_reranker
from app.retrieval import RetrievalConfig, create_retriever
from app.retrieval.filters import payload_filter
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import Embedder, embedder_from_env
from intellidocs_intelligence.models import Chunk
from metrics import GoldQuery, Metrics, score_ranking
from numpy import random as numpy_random
from pydantic import BaseModel
from qdrant_client import QdrantClient

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = Path(__file__).with_name("sample.jsonl")
SAMPLE_CHUNKS = Path(__file__).with_name("sample_chunks.jsonl")
Model = TypeVar("Model", bound=BaseModel)


@dataclass(frozen=True)
class Configuration:
    name: str
    mode: str
    rerank: bool = False
    filters: bool = False


CONFIGURATIONS = (
    Configuration("dense", "dense"),
    Configuration("sparse", "sparse"),
    Configuration("hybrid", "hybrid"),
    Configuration("hybrid_rerank", "hybrid", rerank=True),
    Configuration("hybrid_rerank_filters", "hybrid", rerank=True, filters=True),
)


@dataclass(frozen=True)
class QueryScore:
    id: str
    language: str
    query_type: str
    targets: int
    returned: int
    filters: dict[str, str] | None
    metrics: Metrics


class RunReport(TypedDict):
    config: dict[str, object]
    overall: dict[str, int | float]
    by_language: dict[str, dict[str, int | float]]
    by_query_type: dict[str, dict[str, int | float]]
    per_query: list[dict[str, object]]


def read_jsonl(path: Path, model: type[Model]) -> tuple[list[Model], str]:
    source = path.read_bytes()
    rows = []
    for line_number, line in enumerate(source.decode("utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            rows.append(model.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: {error}") from error
    if not rows:
        raise ValueError(f"{path} contains no JSONL records")
    return rows, hashlib.sha256(source).hexdigest()


def load_gold(path: Path) -> tuple[list[GoldQuery], str]:
    queries, digest = read_jsonl(path, GoldQuery)
    if len({query.id for query in queries}) != len(queries):
        raise ValueError(f"{path} contains duplicate query IDs")
    for query in queries:
        try:
            payload_filter(query.filters)
        except ValueError as error:
            raise ValueError(f"{path}: filters for query {query.id}: {error}") from error
    return queries, digest


def summarize(rows: Sequence[QueryScore]) -> dict[str, int | float]:
    return {
        "queries": len(rows),
        "recall_at_5": mean(row.metrics["recall_at_5"] for row in rows),
        "recall_at_10": mean(row.metrics["recall_at_10"] for row in rows),
        "mrr": mean(row.metrics["mrr"] for row in rows),
        "ndcg_at_10": mean(row.metrics["ndcg_at_10"] for row in rows),
    }


def evaluate(
    gold: Sequence[GoldQuery],
    store: QdrantStore,
    retrieval: RetrievalConfig,
    reranking: RerankerConfig,
) -> dict[str, RunReport]:
    reranker = create_reranker(reranking)
    results: dict[str, RunReport] = {}
    for configuration in CONFIGURATIONS:
        settings = replace(retrieval, mode=configuration.mode)
        retriever = create_retriever(store, settings)
        rows = []
        for entry in gold:
            filters = entry.filters if configuration.filters else None
            pool = retriever.retrieve_pool(entry.query, filters)
            ranking = (
                reranker.rerank(entry.query, pool, len(pool)) if configuration.rerank else pool
            )
            rows.append(
                QueryScore(
                    id=entry.id,
                    language=entry.language,
                    query_type=entry.query_type,
                    targets=len(entry.targets),
                    returned=len(ranking),
                    filters=filters,
                    metrics=score_ranking(
                        [candidate.chunk for candidate in ranking], entry.targets
                    ),
                )
            )
        results[configuration.name] = {
            "config": {
                **asdict(configuration),
                "retrieval": asdict(settings),
                "reranker": type(reranker).__name__ if configuration.rerank else None,
            },
            "overall": summarize(rows),
            "by_language": {
                language: summarize([row for row in rows if row.language == language])
                for language in sorted({query.language for query in gold})
            },
            "by_query_type": {
                kind: summarize([row for row in rows if row.query_type == kind])
                for kind in sorted({query.query_type for query in gold})
            },
            "per_query": [asdict(row) for row in rows],
        }
    return results


@contextmanager
def open_store(
    embedder: Embedder, chunks_path: Path | None
) -> Iterator[tuple[QdrantStore, dict[str, object]]]:
    if chunks_path is None:
        store = QdrantStore.from_env(embedder)
        try:
            if not store.client.collection_exists(store.collection_name):
                raise ValueError(f"Qdrant collection does not exist: {store.collection_name}")
            yield (
                store,
                {
                    "source": "existing_index",
                    "collection": store.collection_name,
                    "points": store.client.count(store.collection_name, exact=True).count,
                },
            )
        finally:
            store.client.close()
        return

    chunks, digest = read_jsonl(chunks_path, Chunk)
    batch_size = int(os.environ.get("EMBEDDING_BATCH_SIZE", "32"))
    if batch_size < 1:
        raise ValueError("EMBEDDING_BATCH_SIZE must be positive")
    client = QdrantClient(":memory:")
    try:
        store = QdrantStore(client, "retrieval_eval", embedder)
        for start in range(0, len(chunks), batch_size):
            store.upsert_chunks(chunks[start : start + batch_size])
        points = client.count(store.collection_name, exact=True).count
        if points != len(chunks):
            raise ValueError(f"{chunks_path} contains duplicate document spans")
        yield (
            store,
            {
                "source": "memory",
                "path": str(chunks_path),
                "sha256": digest,
                "points": points,
                "embedding_batch_size": batch_size,
            },
        )
    finally:
        client.close()


def set_seeds(seed: int, use_torch: bool) -> None:
    if not 0 <= seed < 2**32:
        raise ValueError("Seed must be an unsigned 32-bit integer")
    random.seed(seed)
    numpy_random.seed(seed)
    if use_torch:
        import_module("torch").manual_seed(seed)


def comparison(results: dict[str, RunReport]) -> str:
    lines = [
        f"{'Group':<24} {'Configuration':<24} {'N':>4} "
        f"{'Recall@5':>9} {'Recall@10':>10} {'MRR':>8} {'nDCG@10':>9}"
    ]

    def add(group: str, name: str, row: dict[str, int | float]) -> None:
        lines.append(
            f"{group:<24} {name:<24} {row['queries']:>4} {row['recall_at_5']:>9.3f} "
            f"{row['recall_at_10']:>10.3f} {row['mrr']:>8.3f} {row['ndcg_at_10']:>9.3f}"
        )

    for name, run in results.items():
        add("overall", name, run["overall"])
    first = next(iter(results.values()))
    for language in first["by_language"]:
        for name, run in results.items():
            add(f"language: {language}", name, run["by_language"][language])
    for kind in first["by_query_type"]:
        for name, run in results.items():
            add(kind, name, run["by_query_type"][kind])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--gold", type=Path, default=SAMPLE, help="Gold JSONL (default: bundled sample)"
    )
    data = cli.add_mutually_exclusive_group()
    data.add_argument("--chunks", type=Path, help="Index Chunk v1 JSONL in memory")
    data.add_argument("--existing-index", action="store_true", help="Query QDRANT_URL/collection")
    cli.add_argument("--seed", type=int, default=42)
    cli.add_argument("--output", type=Path)
    cli.add_argument("--overwrite", action="store_true")
    args = cli.parse_args(argv)
    now = datetime.now(UTC)
    output = args.output or ROOT / "eval/results" / f"retrieval_{now.date().isoformat()}.json"
    try:
        if output.exists() and not args.overwrite:
            raise ValueError(f"Result already exists: {output}. Use --output or --overwrite.")
        if args.gold.resolve() != SAMPLE.resolve() and not (args.chunks or args.existing_index):
            raise ValueError("A custom gold set requires --chunks or --existing-index")
        gold, digest = load_gold(args.gold)
        retrieval = RetrievalConfig.from_env()
        reranking = RerankerConfig.from_env(
            {**os.environ, "RERANKER_BACKEND": os.environ.get("RERANKER_BACKEND", "fake")}
        )
        if reranking.backend == "none":
            raise ValueError("Choose fake or cross_encoder for RERANKER_BACKEND in this comparison")
        backend = os.environ.get("EMBEDDING_BACKEND", "fake")
        set_seeds(args.seed, backend != "fake" or reranking.backend == "cross_encoder")
        embedder = embedder_from_env()
        chunks_path = None if args.existing_index else (args.chunks or SAMPLE_CHUNKS)
        with open_store(embedder, chunks_path) as (store, corpus):
            results = evaluate(gold, store, retrieval, reranking)
        report = {
            "created_at": now.isoformat(),
            "gold": {
                "path": str(args.gold),
                "sha256": digest,
                "queries": len(gold),
                "queries_with_filters": sum(bool(entry.filters) for entry in gold),
            },
            "corpus": corpus,
            "config": {
                "seed": args.seed,
                "qdrant_client_version": version("qdrant-client"),
                "embedding_backend": backend,
                "embedding": embedder.cache_key(),
                "embedding_query_prompt": os.environ.get("EMBEDDING_QUERY_PROMPT", ""),
                "reranking": asdict(reranking),
                "ranking_depth": "full_retrieval_pool",
                "recall_cutoffs": [5, 10],
                "ndcg_cutoff": 10,
                "averaging": "macro_by_query",
            },
            "runs": results,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if args.overwrite else "x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        print(comparison(results))
        print(f"Results written to {output}")
        return 0
    except Exception as error:
        print(f"Retrieval evaluation failed. {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
