"""Score OCR, extraction, relations, and role-aware summaries."""

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Self

import yaml
from app.chunking import canonical_document
from app.extraction.service import extraction_from_env
from app.parsing.factory import parser_from_config
from app.parsing.formats import parse_non_email
from app.relations.service import relations_from_env
from app.storage import LocalStorage
from app.summarization.service import summarizer_from_env
from intellidocs_intelligence.models import (
    ExtractionResult,
    OrgConfig,
    ParseResult,
    RelationResult,
    Summary,
    SummaryRequest,
)
from pydantic import Field, model_validator

from .answers.answer_metrics import AnswerPoint, normalized, texts
from .extraction.extraction_metrics import Counts, GoldDocument, faithful, score_document, summarize
from .ocr import evaluate as ocr
from .relations.evaluate_relations import GoldPair, evaluate, rate
from .retrieval.evaluate_retrieval import read_jsonl
from .suite_config import Settings, SuiteConfig, fingerprint, path
from .suite_report import Report


def evaluate_ocr(config: SuiteConfig, report: Report) -> None:
    inputs = config.inputs.ocr
    org_path = path(inputs.org_config)
    org = OrgConfig.model_validate(yaml.safe_load(org_path.read_bytes()))
    parser = parser_from_config(org)
    manifest = path(inputs.manifest)
    samples = ocr.discover(path(inputs.documents), path(inputs.ground_truth), manifest)

    def parse(source: Path) -> ParseResult:
        with report.time("ocr", "full", "parse", "documents"):
            return parse_non_email(source, org.languages, parser, 20 * 1024 * 1024)

    result = ocr.evaluate(samples, parse, org.languages)
    if not result["complete"]:
        errors = "; ".join(
            f"{row['document']}: {row['error']}"
            for row in result["documents"]
            if row["status"] == "error"
        )
        raise ValueError(f"OCR evaluation incomplete: {errors}")
    report.stages["ocr"] = {
        "mode": "live OCR on local documents",
        "engine": org.ocr_engine,
        "org_config": fingerprint(org_path),
        "manifest": fingerprint(manifest),
        "documents": [
            {
                name: row[name]
                for name in (
                    "document",
                    "document_sha256",
                    "references",
                    "parser_name",
                    "parser_version",
                )
            }
            for row in result["documents"]
        ],
    }
    groups = {"overall": result, **{f"language={k}": v for k, v in result["by_language"].items()}}
    for name, group in groups.items():
        for error_rate in ("cer", "wer"):
            report.add(
                "ocr",
                "full",
                name,
                error_rate,
                group["metrics"][error_rate],
                group["documents_scored"],
                "documents",
            )


EXTRACTION_RATES = {
    "precision": ("predictions", "predicted obligations"),
    "recall": ("gold", "gold obligations"),
    "f1": ("obligation_union", "distinct predicted or gold obligations"),
    "owner_accuracy": ("matched", "matched obligations"),
    "deadline_accuracy": ("matched", "matched obligations"),
    "citation_faithfulness": ("citations", "citations"),
}


def _storage(config: SuiteConfig) -> LocalStorage:
    assert config.live_index is not None
    return LocalStorage(path(config.live_index.storage_root))


def evaluate_extraction(config: SuiteConfig, report: Report) -> None:
    inputs = config.inputs.extraction
    gold, _ = read_jsonl(path(inputs.gold), GoldDocument)
    ids = {row.doc_id for row in gold}
    inputs_metadata: dict[str, object] = {"gold": fingerprint(path(inputs.gold))}
    if len(ids) != len(gold):
        raise ValueError("Duplicate extraction gold document IDs")
    if inputs.predictions and inputs.sources:
        predicted, _ = read_jsonl(path(inputs.predictions), ExtractionResult)
        parsed, _ = read_jsonl(path(inputs.sources), ParseResult)
        mode = "snapshot scoring"
        inputs_metadata.update(
            predictions=fingerprint(path(inputs.predictions)),
            sources=fingerprint(path(inputs.sources)),
        )
    else:
        mode = "live extraction and scoring"
        storage = _storage(config)
        parsed = [storage.load_result(row.doc_id) for row in gold]
        predicted = []
        with extraction_from_env(storage) as service:
            for row in gold:
                with report.time("extraction", "full", "extract", "documents"):
                    predicted.append(service.extract(row.doc_id))
    sources = {row.document.id: row.document for row in parsed}
    predictions = {row.doc_id: row for row in predicted}
    if (
        sources.keys() != ids
        or predictions.keys() != ids
        or len(parsed) != len(ids)
        or len(predicted) != len(ids)
    ):
        raise ValueError("Extraction needs exactly one source and prediction per gold document")
    groups: dict[str, list[Counts]] = defaultdict(list)
    for row in gold:
        with report.time("extraction", "full", "score", "documents"):
            counts = score_document(
                row,
                predictions[row.doc_id],
                sources[row.doc_id],
                config.thresholds.extraction_action_similarity,
            )
        for group in ("overall", f"language={row.language}"):
            groups[group].append(counts)
    for group, subset in groups.items():
        totals = summarize(subset)
        totals["obligation_union"] = sum(row.predictions + row.gold - row.matched for row in subset)
        report.rates("extraction", "full", group, totals, EXTRACTION_RATES)
    report.stages["extraction"] = {"mode": mode, "inputs": inputs_metadata}


def evaluate_relations(config: SuiteConfig, report: Report) -> None:
    inputs = config.inputs.relations
    gold, _ = read_jsonl(path(inputs.gold), GoldPair)
    inputs_metadata: dict[str, object] = {"gold": fingerprint(path(inputs.gold))}
    if inputs.predictions and inputs.sources:
        predicted, _ = read_jsonl(path(inputs.predictions), RelationResult)
        sources, _ = read_jsonl(path(inputs.sources), ParseResult)
        mode = "snapshot scoring"
        inputs_metadata.update(
            predictions=fingerprint(path(inputs.predictions)),
            sources=fingerprint(path(inputs.sources)),
        )
    else:
        mode = "live relation detection and scoring"
        storage = _storage(config)
        sources = [
            storage.load_result(doc_id)
            for doc_id in sorted({key for row in gold for key in row.pair})
        ]
        predicted = []
        with relations_from_env(storage) as service:
            for doc_id in sorted({row.source_doc_id for row in gold}):
                with report.time("relations", "full", "detect", "source documents"):
                    predicted.append(service.detect(doc_id))
    with report.time("relations", "full", "score", "gold set batches"):
        scores = evaluate(gold, predicted, sources)
    for group, totals in [("overall", scores["overall"]), *scores["by_relation_type"].items()]:
        report.add(
            "relations",
            "full",
            group,
            "f1",
            totals["f1"],
            totals["tp"] + totals["fp"] + totals["fn"],
            "distinct predicted or gold relations",
        )
    for metric, counts in (
        ("false_supersession", scores["false_supersession"]),
        ("confirmed_false_supersession", scores["confirmed_false_supersession"]),
    ):
        report.add(
            "relations",
            "full",
            "overall",
            f"{metric}_rate",
            counts["rate"],
            counts["eligible_documents"],
            "eligible target documents",
        )
    report.add(
        "relations",
        "full",
        "overall",
        "citation_faithfulness",
        scores["citation_faithfulness"],
        scores["citations"],
        "citations",
    )
    report.stages["relations"] = {"mode": mode, "inputs": inputs_metadata}


class GoldSummary(Settings):
    id: Annotated[str, Field(pattern=r"\S")]
    request: SummaryRequest
    expected_points: list[AnswerPoint | Annotated[str, Field(pattern=r"\S")]]
    not_relevant: bool

    @model_validator(mode="after")
    def validate_points(self) -> Self:
        if self.not_relevant == bool(self.expected_points):
            raise ValueError("Expected points must be present exactly for relevant summaries")
        labels = [normalized(label) for point in self.expected_points for label in texts(point)]
        if len(set(labels)) != len(labels):
            raise ValueError("Summary point texts and aliases must be distinct")
        return self


def _summary_rates(
    gold: Sequence[GoldSummary],
    results: dict[str, Summary],
    sources: dict[str, ParseResult],
    report: Report,
) -> None:
    totals: dict[str, int] = defaultdict(int)
    canonical = {doc_id: canonical_document(source.document) for doc_id, source in sources.items()}
    for row in gold:
        with report.time("summary", "full", "score", "requests"):
            request, result = row.request, results[row.id]
            if (
                result.doc_id,
                result.language,
                result.audience.department,
                result.audience.role,
            ) != (
                request.doc_id,
                request.language,
                request.department,
                request.role,
            ):
                raise ValueError(f"Summary document, language or audience mismatch for {row.id}")
            supported = set()
            for point in result.points:
                cited = sum(
                    faithful(cite, result.doc_id, canonical[result.doc_id])
                    for cite in point.citations
                )
                totals["citations"] += len(point.citations)
                totals["faithful"] += cited
                if cited:
                    supported.add(normalized(point.text))
            totals["expected"] += len(row.expected_points)
            totals["covered"] += sum(
                any(normalized(option) in supported for option in texts(point))
                for point in row.expected_points
            )
            totals["irrelevant"] += int(row.not_relevant)
            totals["abstained"] += int(row.not_relevant and result.not_relevant)
    for name, numerator, denominator, unit in (
        ("citation_faithfulness", "faithful", "citations", "citations"),
        ("point_coverage", "covered", "expected", "expected points"),
        ("abstention_rate_irrelevant", "abstained", "irrelevant", "irrelevant requests"),
    ):
        report.add(
            "summary",
            "full",
            "overall",
            name,
            rate(totals[numerator], totals[denominator]),
            totals[denominator],
            unit,
        )


def evaluate_summary(config: SuiteConfig, report: Report) -> None:
    inputs = config.inputs.summary
    gold, _ = read_jsonl(path(inputs.gold), GoldSummary)
    ids = {row.id for row in gold}
    if len(ids) != len(gold):
        raise ValueError("Duplicate summary gold request IDs")
    doc_ids = {row.request.doc_id for row in gold}
    inputs_metadata: dict[str, object] = {"gold": fingerprint(path(inputs.gold))}
    if inputs.predictions and inputs.sources:
        raw = json.loads(path(inputs.predictions).read_bytes())
        predictions = {key: Summary.model_validate(value) for key, value in raw.items()}
        parsed, _ = read_jsonl(path(inputs.sources), ParseResult)
        mode = "snapshot scoring"
        inputs_metadata.update(
            predictions=fingerprint(path(inputs.predictions)),
            sources=fingerprint(path(inputs.sources)),
        )
    else:
        mode = "live summarization and scoring"
        storage = _storage(config)
        parsed = [storage.load_result(doc_id) for doc_id in sorted(doc_ids)]
        predictions = {}
        with summarizer_from_env(storage) as service:
            for row in gold:
                with report.time("summary", "full", "summarize", "requests"):
                    predictions[row.id] = service.summarize(
                        row.request,
                        assigned_obligations=row.request.assigned_obligations,
                    )
    sources = {row.document.id: row for row in parsed}
    if predictions.keys() != ids or len(sources) != len(parsed) or not doc_ids <= sources.keys():
        raise ValueError("Summaries need one prediction per request and every requested source")
    _summary_rates(gold, predictions, sources, report)
    report.stages["summary"] = {"mode": mode, "inputs": inputs_metadata}
