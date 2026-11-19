"""Evaluate v1 relation results and trace both sides to stored parsed text."""

import argparse
import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TypeVar, get_args

from app.chunking.canonical import canonical_document
from app.relations.service import relations_from_env
from app.storage import LocalStorage
from intellidocs_intelligence.models import (
    DocumentRelation,
    DocumentRelationType,
    ParseResult,
    RelationResult,
    RelationScope,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eval.extraction.extraction_metrics import faithful

HERE = Path(__file__).resolve().parent
KINDS: tuple[DocumentRelationType, ...] = get_args(DocumentRelationType)
Model = TypeVar("Model", bound=BaseModel)
Page = Annotated[int, Field(ge=1)]
Pair = tuple[str, str]
RelationKey = tuple[str, str, DocumentRelationType, RelationScope]


class GoldPair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    source_doc_id: str = Field(min_length=1)
    target_doc_id: str = Field(min_length=1)
    relation_type: DocumentRelationType | None
    scope: RelationScope | None
    source_pages: list[Page]
    target_pages: list[Page]

    @model_validator(mode="after")
    def check_pair(self) -> "GoldPair":
        if self.source_doc_id == self.target_doc_id:
            raise ValueError("Gold pairs require distinct documents")
        if self.relation_type is None:
            if self.scope is not None or self.source_pages or self.target_pages:
                raise ValueError(
                    "A negative pair requires null scope and no evidence pages"
                )
        elif self.scope is None or not self.source_pages or not self.target_pages:
            raise ValueError(
                "A positive pair requires scope and evidence pages for both documents"
            )
        if any(
            len(pages) != len(set(pages))
            for pages in (self.source_pages, self.target_pages)
        ):
            raise ValueError("Evidence pages must be distinct for each document")
        return self

    @property
    def pair(self) -> Pair:
        return self.source_doc_id, self.target_doc_id

    @property
    def key(self) -> RelationKey | None:
        if self.relation_type is None or self.scope is None:
            return None
        return *self.pair, self.relation_type, self.scope


def read_jsonl(path: Path, model: type[Model]) -> tuple[list[Model], dict[str, str]]:
    data = path.read_bytes()
    items = []
    for line_no, line in enumerate(data.decode("utf-8").split("\n"), 1):
        if line.strip():
            try:
                items.append(model.model_validate_json(line))
            except ValueError as error:
                raise ValueError(f"{path}:{line_no}: {error}") from error
    if not items:
        raise ValueError(f"{path} contains no JSONL records")
    return items, {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def classification(
    expected: set[RelationKey], predicted: set[RelationKey]
) -> dict[str, object]:
    tp = len(expected & predicted)
    fp = len(predicted - expected)
    fn = len(expected - predicted)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": rate(tp, tp + fp),
        "recall": rate(tp, tp + fn),
        "f1": rate(2 * tp, 2 * tp + fp + fn),
    }


def fully_stale(keys: set[RelationKey]) -> set[str]:
    return {
        target
        for _, target, kind, scope in keys
        if kind in {"supersedes", "withdraws"} and scope == "whole_document"
    }


def evaluate(
    gold: Sequence[GoldPair],
    predictions: Sequence[RelationResult],
    sources: Sequence[ParseResult],
    *,
    confirmed_only: bool = False,
) -> dict[str, object]:
    parsed = {row.document.id: row.document for row in sources}
    if len(parsed) != len(sources):
        raise ValueError("Source document IDs must be unique")
    pairs: dict[Pair, set[RelationKey | None]] = defaultdict(set)
    expected: dict[RelationKey, GoldPair] = {}
    for label in gold:
        key = label.key
        if key in pairs[label.pair]:
            raise ValueError(f"Duplicate gold relation: {key or label.pair}")
        pairs[label.pair].add(key)
        for doc_id, pages in zip(
            label.pair, (label.source_pages, label.target_pages), strict=True
        ):
            if doc_id not in parsed:
                raise ValueError(f"Gold document is missing from sources: {doc_id}")
            if not set(pages) <= {page.page for page in parsed[doc_id].pages}:
                raise ValueError(f"Gold evidence page is missing in source: {doc_id}")
        if key:
            expected[key] = label
    if any(None in keys and len(keys) > 1 for keys in pairs.values()):
        raise ValueError(
            "A gold pair cannot have both a relation and a no-relation label"
        )
    results = {row.doc_id: row for row in predictions}
    if len(results) != len(predictions) or results.keys() != {
        source for source, _ in pairs
    }:
        raise ValueError("Supply exactly one RelationResult per gold source_doc_id")
    canonical = {
        doc_id: canonical_document(document) for doc_id, document in parsed.items()
    }
    predicted: dict[RelationKey, list[DocumentRelation]] = defaultdict(list)
    confirmed: set[RelationKey] = set()
    seen_ids: set[str] = set()
    for result in predictions:
        for relation in result.relations:
            pair = relation.source_doc_id, relation.target_doc_id
            if relation.source_doc_id != result.doc_id or relation.id in seen_ids:
                raise ValueError(f"Misplaced or duplicate relation: {relation.id}")
            seen_ids.add(relation.id)
            if relation.status == "dismissed":
                continue
            if pair not in pairs:
                raise ValueError(f"Missing gold label for predicted pair: {pair}")
            key = *pair, relation.relation_type, relation.scope
            if relation.status == "confirmed":
                confirmed.add(key)
            if not confirmed_only or relation.status == "confirmed":
                predicted[key].append(relation)

    faithful_count = citation_count = complete_evidence = 0
    failed_citations = []
    for key, members in predicted.items():
        covered: tuple[set[int], set[int]] = set(), set()
        for relation in members:
            for side, (doc_id, citations) in enumerate(
                (
                    (relation.source_doc_id, relation.source_citations),
                    (relation.target_doc_id, relation.target_citations),
                )
            ):
                for citation in citations:
                    citation_count += 1
                    source = canonical[doc_id]
                    if faithful(citation, doc_id, source):
                        faithful_count += 1
                        covered[side].update(
                            span.page
                            for span in source.overlapping_blocks(
                                citation.char_start, citation.char_end
                            )
                        )
                    else:
                        failed_citations.append(
                            {
                                "relation_id": relation.id,
                                "doc_id": doc_id,
                                "page": citation.page,
                                "char_start": citation.char_start,
                                "char_end": citation.char_end,
                            }
                        )
        label = expected.get(key)
        if (
            label
            and set(label.source_pages) <= covered[0]
            and set(label.target_pages) <= covered[1]
        ):
            complete_evidence += 1

    expected_keys, predicted_keys = set(expected), set(predicted)
    eligible_targets = {target for _, target in pairs} - fully_stale(expected_keys)

    def stale_stats(keys: set[RelationKey]) -> dict[str, object]:
        wrong = fully_stale(keys) & eligible_targets
        return {
            "rate": rate(len(wrong), len(eligible_targets)),
            "false_documents": sorted(wrong),
            "eligible_documents": len(eligible_targets),
        }

    return {
        "overall": classification(expected_keys, predicted_keys),
        "by_relation_type": {
            kind: classification(
                {key for key in expected if key[2] == kind},
                {key for key in predicted if key[2] == kind},
            )
            for kind in KINDS
        },
        "false_supersession": stale_stats(predicted_keys),
        "confirmed_false_supersession": stale_stats(confirmed),
        "citation_faithfulness": rate(faithful_count, citation_count),
        "faithful_citations": faithful_count,
        "citations": citation_count,
        "failed_citations": failed_citations,
        "gold_evidence_page_coverage": rate(complete_evidence, len(expected_keys)),
        "relations_covering_gold_evidence_pages": complete_evidence,
        "predictions_missing_evidence": sum(
            not any(row.source_citations for row in rows)
            or not any(row.target_citations for row in rows)
            for rows in predicted.values()
        ),
        "missed_relations": sorted(expected_keys - predicted_keys),
        "unexpected_relations": sorted(predicted_keys - expected_keys),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=HERE / "sample.jsonl")
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--sources", type=Path)
    parser.add_argument(
        "--storage-root", type=Path, help="Run detection on the configured index"
    )
    parser.add_argument("--confirmed-only", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.storage_root and (args.predictions or args.sources or args.confirmed_only):
        parser.error(
            "--storage-root runs detection; omit snapshots and --confirmed-only"
        )
    now = datetime.now(UTC)
    output = args.output or HERE.parent / "results" / f"relations_{now.date()}.json"
    try:
        if output.exists() and not args.overwrite:
            raise FileExistsError(
                f"{output} already exists. Choose --output or use --overwrite."
            )
        gold, gold_file = read_jsonl(args.gold, GoldPair)
        inputs: dict[str, object] = {"gold": gold_file}
        if args.storage_root:
            storage = LocalStorage(args.storage_root)
            source_ids = sorted({row.source_doc_id for row in gold})
            sources = [
                storage.load_result(doc_id)
                for doc_id in sorted({doc_id for row in gold for doc_id in row.pair})
            ]
            with relations_from_env(storage) as service:
                predictions = [service.detect(doc_id) for doc_id in source_ids]
            inputs["storage_root"] = str(args.storage_root)
        else:
            sources, inputs["sources"] = read_jsonl(
                args.sources or HERE / "sample_sources.jsonl", ParseResult
            )
            predictions, inputs["predictions"] = read_jsonl(
                args.predictions or HERE / "sample_predictions.jsonl", RelationResult
            )
        scores = evaluate(
            gold, predictions, sources, confirmed_only=args.confirmed_only
        )
        report = {
            "evaluated_at": now.isoformat(),
            "inputs": inputs,
            "prediction_statuses": ["confirmed"]
            if args.confirmed_only
            else ["proposed", "confirmed"],
            "scores": scores,
            "detector_warnings": {
                result.doc_id: result.warnings
                for result in predictions
                if result.warnings
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if args.overwrite else "x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")

    def display(value: object) -> str:
        return f"{value:.1%}" if isinstance(value, float) else "n/a"

    print(f"{'Relation type':<18} {'Precision':>9} {'Recall':>9} {'F1':>9}")
    by_kind = scores["by_relation_type"]
    assert isinstance(by_kind, dict)
    for kind, row in by_kind.items():
        print(
            f"{kind:<18} "
            + " ".join(
                f"{display(row[name]):>9}" for name in ("precision", "recall", "f1")
            )
        )
    for key, label in (
        ("false_supersession", "False supersession rate"),
        ("confirmed_false_supersession", "Confirmed false supersession rate"),
    ):
        metric = scores[key]
        assert isinstance(metric, dict)
        print(f"{label}: {display(metric['rate'])}")
    print(f"Citation faithfulness: {display(scores['citation_faithfulness'])}")
    print(
        f"Gold evidence page coverage: {display(scores['gold_evidence_page_coverage'])}"
    )
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
