"""Evaluate the answer pipeline with paired verified and verifier-off outputs."""

import argparse
import hashlib
import json
import os
import random
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from answer_metrics import GoldAnswer, ScoredClaim, baseline_claims, score, summarize
from pydantic import BaseModel
from qdrant_client import QdrantClient

from app.answering import AnswerService
from app.generation import ClaimGenerator, GenerationConfig, GenerationResult, generator_from_env
from app.reranking import RerankerConfig, create_reranker
from app.retrieval import RetrievalConfig, create_retriever
from app.retrieval.filters import payload_filter
from app.search import SearchService
from app.storage import LocalStorage
from app.vector_store import QdrantStore
from app.verification import CitationVerifier, VerificationConfig, verifier_from_env
from intellidocs_intelligence.embeddings import Embedder, configured_embedder
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.models import AnswerRequest, Chunk

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = Path(__file__).with_name("sample.jsonl")
SAMPLE_CHUNKS = Path(__file__).with_name("sample_chunks.jsonl")
Model = TypeVar("Model", bound=BaseModel)


def read_jsonl(path: Path, model: type[Model]) -> tuple[list[Model], str]:
    content = path.read_bytes()
    rows = []
    for number, line in enumerate(content.decode("utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(model.model_validate_json(line))
            except ValueError as error:
                raise ValueError(f"{path}:{number}: {error}") from error
    if not rows:
        raise ValueError(f"{path} contains no JSONL records")
    return rows, hashlib.sha256(content).hexdigest()


def load_gold(path: Path) -> tuple[list[GoldAnswer], str]:
    gold, digest = read_jsonl(path, GoldAnswer)
    if len({row.id for row in gold}) != len(gold):
        raise ValueError(f"{path} contains duplicate query IDs")
    for row in gold:
        payload_filter(row.filters)
    return gold, digest


@contextmanager
def open_store(embedder: Embedder, chunks: Path | None) -> Iterator[tuple[QdrantStore, dict]]:
    client = QdrantClient(":memory:") if chunks else None
    store = (
        QdrantStore(client, "answer_eval", embedder) if client else QdrantStore.from_env(embedder)
    )
    try:
        metadata: dict = {"source": "existing_index", "collection": store.collection_name}
        if chunks:
            rows, digest = read_jsonl(chunks, Chunk)
            batch = int(os.environ.get("EMBEDDING_BATCH_SIZE", "32"))
            if batch < 1:
                raise ValueError("EMBEDDING_BATCH_SIZE must be positive")
            for start in range(0, len(rows), batch):
                store.upsert_chunks(rows[start : start + batch])
            metadata = {"source": "memory", "path": str(chunks), "sha256": digest}
        if not store.client.collection_exists(store.collection_name):
            raise ValueError("Qdrant collection does not exist")
        metadata["points"] = store.client.count(store.collection_name, exact=True).count
        yield store, metadata
    finally:
        store.client.close()


def evaluate(
    gold: Sequence[GoldAnswer],
    service: AnswerService,
    *,
    mode: str = "both",
    top_k: int = 10,
    language: str | None = None,
    responses: dict[str, GenerationResult] | None = None,
    fixture: FakeProvider | None = None,
) -> dict:
    runs: dict[str, dict] = {
        name: {"per_query": []} for name in ("baseline", "verified") if mode in {"both", name}
    }
    for row in gold:
        if responses is not None and fixture is not None:
            fixture.response = responses[row.id].model_dump(mode="json")
        requested_language = row.language or language
        if requested_language is None:
            raise ValueError(f"Query {row.id} needs language or --language")
        draft = service.prepare(
            AnswerRequest(
                query=row.query, language=requested_language, top_k=top_k, filters=row.filters
            )
        )
        outputs = {}
        if "baseline" in runs:
            outputs["baseline"] = baseline_claims(draft)
        if "verified" in runs:
            answer = service.finalize(draft)
            outputs["verified"] = [
                ScoredClaim(claim.text, claim.citations) for claim in answer.claims
            ]
        for name, claims in outputs.items():
            runs[name]["per_query"].append(
                {
                    "id": row.id,
                    "metrics": score(row, claims, draft),
                    "generation_failed": draft.generation_failed,
                    "retrieved_chunk_ids": [chunk.chunk_id or chunk.id for chunk in draft.chunks],
                    "claims": [
                        {
                            "text": claim.text,
                            "citations": [
                                citation.model_dump(mode="json", exclude_none=True)
                                if citation
                                else None
                                for citation in claim.citations
                            ],
                        }
                        for claim in claims
                    ],
                }
            )
    for run in runs.values():
        run["overall"] = summarize([row["metrics"] for row in run["per_query"]])
    return runs


def comparison(runs: dict) -> str:
    lines = ["Mode       Citation precision  Unsupported claims  Unanswerable abstention  Coverage"]
    for name, run in runs.items():
        metrics = run["overall"]
        values = [
            "n/a" if metrics[key] is None else f"{metrics[key]:.3f}"
            for key in (
                "citation_precision",
                "unsupported_claim_rate",
                "abstention_rate_unanswerable",
                "answer_coverage",
            )
        ]
        lines.append(f"{name:<10} {values[0]:>18} {values[1]:>19} {values[2]:>24} {values[3]:>9}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--gold", type=Path, default=SAMPLE)
    sources = cli.add_mutually_exclusive_group()
    sources.add_argument("--chunks", type=Path)
    sources.add_argument("--existing-index", action="store_true")
    cli.add_argument("--storage-root", type=Path, help="Stored ParseResult directory (STORAGE_DIR)")
    cli.add_argument(
        "--responses", type=Path, help="Offline fixture JSON mapping query IDs to claims"
    )
    cli.add_argument("--mode", choices=["both", "baseline", "verified"], default="both")
    cli.add_argument(
        "--language", help="Explicit answer language for gold entries without language"
    )
    cli.add_argument("--top-k", type=int)
    cli.add_argument("--seed", type=int, default=42)
    cli.add_argument("--output", type=Path)
    cli.add_argument("--overwrite", action="store_true")
    args = cli.parse_args(argv)
    now = datetime.now(UTC)
    output = args.output or ROOT / "eval/results" / f"answers_{now.date().isoformat()}.json"
    try:
        if output.exists() and not args.overwrite:
            raise ValueError(f"Result already exists: {output}. Use --output or --overwrite.")
        if args.gold.resolve() != SAMPLE.resolve() and not (args.chunks or args.existing_index):
            raise ValueError("Custom gold requires --chunks or --existing-index")
        random.seed(args.seed)
        gold, digest = load_gold(args.gold)
        retrieval, reranking = RetrievalConfig.from_env(), RerankerConfig.from_env()
        fixture = None
        responses = None
        if args.responses:
            raw = json.loads(args.responses.read_text(encoding="utf-8"))
            if set(raw) != {row.id for row in gold}:
                raise ValueError("Fixture responses must have exactly the gold query IDs")
            responses = {key: GenerationResult.model_validate(value) for key, value in raw.items()}
            fixture = FakeProvider(model="fixture-claims.v1")
            generator = ClaimGenerator(fixture)
        else:
            generator = generator_from_env()
        embedder = configured_embedder()
        chunks = None if args.existing_index else (args.chunks or SAMPLE_CHUNKS)
        storage_root = args.storage_root or (
            os.environ.get("STORAGE_DIR") if args.existing_index else None
        )
        if args.existing_index and not storage_root:
            raise ValueError("--existing-index requires --storage-root or STORAGE_DIR")
        verifier = verifier_from_env() if args.mode != "baseline" else None
        with open_store(embedder, chunks) as (store, corpus):
            service = AnswerService(
                SearchService(create_retriever(store, retrieval), create_reranker(reranking)),
                generator,
                # Baseline never calls the verifier or its optional support provider.
                verifier or CitationVerifier(VerificationConfig()),
                LocalStorage(Path(storage_root)) if storage_root else None,
            )
            runs = evaluate(
                gold,
                service,
                mode=args.mode,
                top_k=args.top_k if args.top_k is not None else retrieval.default_top_k,
                language=args.language,
                responses=responses,
                fixture=fixture,
            )
        report = {
            "created_at": now.isoformat(),
            "gold": {"path": str(args.gold), "sha256": digest, "queries": len(gold)},
            "corpus": corpus,
            "config": {
                "seed": args.seed,
                "mode": args.mode,
                "model": generator.provider.model,
                "provider": generator.provider.provider_id,
                "generation": asdict(GenerationConfig.from_env()),
                "prompt_hashes": {
                    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for folder in ("generation", "verification")
                    for path in sorted(
                        (ROOT / "services/intelligence/app" / folder / "prompts").glob("*.txt")
                    )
                },
                "retrieval": asdict(retrieval),
                "reranking": asdict(reranking),
                "verification": asdict(VerificationConfig.from_env()),
                "embedding": embedder.cache_key(),
                "top_k": args.top_k if args.top_k is not None else retrieval.default_top_k,
                "language_override": args.language,
                "fixture_responses_sha256": hashlib.sha256(args.responses.read_bytes()).hexdigest()
                if args.responses
                else None,
                "point_matching": "normalized exact text or human-authored alias",
            },
            "runs": runs,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if args.overwrite else "x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        print(comparison(runs))
        print(f"Results written to {output}")
        return 0
    except Exception as error:
        print(f"Answer evaluation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
