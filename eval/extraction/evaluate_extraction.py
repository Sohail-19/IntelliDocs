"""Score saved extraction results or run extraction against an existing index."""

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, TypeVar

from extraction_metrics import Counts, GoldDocument, score_document, summarize
from pydantic import BaseModel

from app.extraction import ExtractionConfig
from app.extraction.service import extraction_from_env
from app.ingestion import load_org, select_org_id
from app.storage import LocalStorage
from intellidocs_intelligence.llm import provider_from_env
from intellidocs_intelligence.models import ExtractionResult, ParseResult

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = Path(__file__).with_name("sample.jsonl")
Model = TypeVar("Model", bound=BaseModel)
METRICS = (
    ("precision", "Precision"),
    ("recall", "Recall"),
    ("f1", "F1"),
    ("owner_accuracy", "Owner acc."),
    ("deadline_accuracy", "Deadline acc."),
    ("citation_faithfulness", "Citation faith."),
    ("needs_review_rate", "Needs review"),
)


class Scores(TypedDict):
    overall: dict[str, int | float | None]
    by_language: dict[str, dict[str, int | float | None]]
    per_document: list[dict[str, str | int | float | None]]


def read_jsonl(path: Path, model: type[Model]) -> tuple[list[Model], dict[str, str]]:
    content = path.read_bytes()
    rows = []
    for number, line in enumerate(content.decode("utf-8").split("\n"), 1):
        if line.strip():
            try:
                rows.append(model.model_validate_json(line))
            except ValueError as error:
                raise ValueError(f"{path}:{number}: {error}") from error
    if not rows:
        raise ValueError(f"{path} contains no JSONL records")
    return rows, {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}


def evaluate(
    gold: Sequence[GoldDocument],
    sources: dict[str, ParseResult],
    predictions: dict[str, ExtractionResult],
    threshold: float,
) -> Scores:
    expected = {row.doc_id for row in gold}
    for name, ids in (("sources", sources.keys()), ("predictions", predictions.keys())):
        if expected != ids:
            missing, extra = sorted(expected - ids), sorted(ids - expected)
            raise ValueError(f"{name} IDs differ from gold: missing={missing}, extra={extra}")

    overall = []
    languages: dict[str, list[Counts]] = defaultdict(list)
    per_document: list[dict[str, str | int | float | None]] = []
    for row in gold:
        try:
            counts = score_document(
                row, predictions[row.doc_id], sources[row.doc_id].document, threshold
            )
        except ValueError as error:
            raise ValueError(f"Document {row.doc_id}: {error}") from error
        language = row.language.casefold()
        overall.append(counts)
        languages[language].append(counts)
        per_document.append({"doc_id": row.doc_id, "language": language, **summarize([counts])})
    return {
        "overall": summarize(overall),
        "by_language": {language: summarize(rows) for language, rows in sorted(languages.items())},
        "per_document": per_document,
    }


def _live_inputs(
    gold: Sequence[GoldDocument], storage_root: Path
) -> tuple[dict[str, ParseResult], dict[str, ExtractionResult], dict[str, object]]:
    storage = LocalStorage(storage_root)
    provider = provider_from_env()
    sources, predictions = {}, {}
    org_ids, org_hashes = {}, {}
    with extraction_from_env(storage) as service:
        for row in gold:
            try:
                parsed = storage.load_result(row.doc_id)
                org_id = select_org_id(parsed.org_id)
                if org_id not in org_hashes:
                    config = load_org(org_id).model_dump(mode="json")
                    raw = json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    org_hashes[org_id] = hashlib.sha256(raw).hexdigest()
                org_ids[row.doc_id] = org_id
                sources[row.doc_id] = parsed
                predictions[row.doc_id] = service.extract(row.doc_id)
            except Exception as error:
                raise RuntimeError(f"Extraction failed for document {row.doc_id}") from error
    return (
        sources,
        predictions,
        {
            "storage_root": str(storage_root),
            "qdrant_collection": os.environ.get("QDRANT_COLLECTION"),
            "llm_provider": provider.provider_id,
            "llm_model": provider.model,
            "extraction": asdict(ExtractionConfig.from_env()),
            "document_org_ids": org_ids,
            "org_config_sha256": org_hashes,
            "source_sha256": {
                doc_id: hashlib.sha256(parsed.model_dump_json().encode("utf-8")).hexdigest()
                for doc_id, parsed in sources.items()
            },
        },
    )


def _table(report: Scores) -> str:
    header = f"{'Group':<12}" + "".join(f"{title:>16}" for _, title in METRICS)
    lines = [header]
    for label, row in [("overall", report["overall"]), *report["by_language"].items()]:
        values = ["n/a" if row[key] is None else f"{row[key]:.3f}" for key, _ in METRICS]
        lines.append(f"{label:<12}" + "".join(f"{value:>16}" for value in values))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=SAMPLE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--predictions", type=Path, help="Saved ExtractionResult JSONL")
    mode.add_argument(
        "--existing-index", action="store_true", help="Extract from configured Qdrant"
    )
    parser.add_argument(
        "--sources", type=Path, help="Saved ParseResult JSONL for the same documents"
    )
    parser.add_argument(
        "--storage-root", type=Path, help="Override STORAGE_DIR in existing index mode"
    )
    parser.add_argument("--similarity-threshold", type=float, default=0.85)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    output = args.output or ROOT / "eval/results" / f"extraction_{now.date().isoformat()}.json"
    try:
        if not 0 < args.similarity_threshold <= 1:
            raise ValueError("--similarity-threshold must be in (0, 1]")
        if output.exists() and not args.overwrite:
            raise ValueError(f"Report already exists: {output}. Use --output or --overwrite.")
        gold, gold_meta = read_jsonl(args.gold, GoldDocument)
        if len({row.doc_id for row in gold}) != len(gold):
            raise ValueError("Gold doc_id values must be unique")
        config: dict[str, object] = {
            "similarity_threshold": args.similarity_threshold,
            "action_similarity": "symmetric SequenceMatcher with NFKC, case and whitespace folding",
            "alignment": "one-to-one maximum cardinality across actions at or above threshold",
            "averaging": "micro over documents",
            "rejected_predictions_in_prf": False,
            "citation_match": "literal quote in canonical offsets and matching document/page range",
            "mode": "existing_index" if args.existing_index else "saved_predictions",
        }
        if args.existing_index:
            if args.sources:
                raise ValueError("--sources applies only to saved predictions")
            root = args.storage_root or os.environ.get("STORAGE_DIR")
            if not root:
                raise ValueError("--existing-index requires --storage-root or STORAGE_DIR")
            sources, predictions, runtime = _live_inputs(gold, Path(root))
            config["runtime"] = runtime
            inputs: dict[str, object] = {}
        else:
            if args.storage_root:
                raise ValueError("--storage-root applies only to --existing-index")
            if args.gold.resolve() != SAMPLE.resolve() and not (args.sources and args.predictions):
                raise ValueError("Custom gold requires both --sources and --predictions")
            parsed, source_meta = read_jsonl(
                args.sources or SAMPLE.with_name("sample_sources.jsonl"), ParseResult
            )
            results, prediction_meta = read_jsonl(
                args.predictions or SAMPLE.with_name("sample_predictions.jsonl"),
                ExtractionResult,
            )
            sources = {item.document.id: item for item in parsed}
            predictions = {item.doc_id: item for item in results}
            if len(sources) != len(parsed) or len(predictions) != len(results):
                raise ValueError("Source and prediction doc_id values must be unique")
            inputs = {"sources": source_meta, "predictions": prediction_meta}
        config["extractor_versions"] = sorted(
            {
                item.extractor_version
                for result in predictions.values()
                for item in result.obligations
                if item.extractor_version
            }
        )
        scores = evaluate(gold, sources, predictions, args.similarity_threshold)
        report = {
            "created_at": now.isoformat(),
            "config": config,
            "inputs": {"gold": gold_meta, **inputs},
            **scores,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if args.overwrite else "x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        print(_table(scores))
        print(f"Results written to {output}")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Extraction evaluation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
