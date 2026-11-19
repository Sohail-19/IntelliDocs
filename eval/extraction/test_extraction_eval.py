"""Hand-checked matching, citation, and report behavior."""

import json
from pathlib import Path

import pytest
from evaluate_extraction import SAMPLE, main, read_jsonl
from extraction_metrics import GoldDocument, faithful, match_actions, score_document, summarize

from app.chunking.canonical import canonical_document
from intellidocs_intelligence.models import Citation, ExtractionResult, ParseResult


def test_matching_reroutes_pairs_and_never_counts_a_gold_action_twice() -> None:
    [english, _] = read_jsonl(SAMPLE, GoldDocument)[0]
    [result, _] = read_jsonl(SAMPLE.with_name("sample_predictions.jsonl"), ExtractionResult)[0]
    exact = result.obligations[0].model_copy(update={"action": "FILE  DRAFT"})
    shorter = exact.model_copy(update={"action": "file draf"})
    labels = [
        label.model_copy(update={"action": action})
        for label, action in zip(english.obligations, ["file draft", "file drafts"], strict=True)
    ]
    pairs = match_actions([exact, shorter, shorter], labels, 0.94)
    assert pairs == [(0, 1), (1, 0)]
    assert match_actions([exact, shorter], labels, 1.0) == [(0, 0)]


def test_citation_checks_the_actual_span_document_and_page() -> None:
    english = read_jsonl(SAMPLE.with_name("sample_sources.jsonl"), ParseResult)[0][0].document
    source = canonical_document(english)
    quote = "Records must retain"
    start = source.text.index(quote)
    citation = Citation(
        doc_id=english.id, page=2, char_start=start, char_end=start + len(quote), quote=quote
    )
    assert faithful(citation, english.id, source)
    for fields in [
        {"char_start": 0, "char_end": len(quote)},
        {"char_end": len(source.text) + 1},
        {"page": 1},
        {"quote": quote.lower()},
        {"doc_id": "different-doc"},
    ]:
        assert not faithful(citation.model_copy(update=fields), english.id, source)


def test_zero_predictions_have_no_precision_and_zero_recall() -> None:
    gold = read_jsonl(SAMPLE, GoldDocument)[0][0]
    source = read_jsonl(SAMPLE.with_name("sample_sources.jsonl"), ParseResult)[0][0].document
    result = ExtractionResult(
        doc_id=gold.doc_id,
        obligations=[],
        warnings=[],
        stats={"total": 0, "extracted": 0, "verified": 0, "needs_review": 0, "rejected": 0},
    )
    report = summarize([score_document(gold, result, source, 0.85)])
    assert report["f1"] == report["recall"] == 0
    assert (
        report["precision"] is report["owner_accuracy"] is report["citation_faithfulness"] is None
    )


def test_sample_report_includes_config_language_groups_and_known_scores(tmp_path: Path) -> None:
    report = tmp_path / "sample.json"
    assert main(["--output", str(report)]) == 0
    payload = json.loads(report.read_text())
    assert payload["config"]["similarity_threshold"] == 0.85
    assert payload["config"]["extractor_versions"] == ["synthetic-v1"]
    assert payload["overall"] == pytest.approx(
        {
            "documents": 2,
            "gold": 3,
            "predictions": 3,
            "matched": 2,
            "rejected": 1,
            "needs_review": 2,
            "owners_correct": 1,
            "deadlines_correct": 1,
            "citations": 3,
            "faithful_citations": 2,
            "uncited": 0,
            "false_positives": 1,
            "false_negatives": 1,
            "precision": 2 / 3,
            "recall": 2 / 3,
            "f1": 2 / 3,
            "owner_accuracy": 0.5,
            "deadline_accuracy": 0.5,
            "citation_faithfulness": 2 / 3,
            "needs_review_rate": 0.5,
        }
    )
    assert payload["by_language"]["en"]["recall"] == 0.5
    assert payload["by_language"]["hi"]["recall"] == 1
    assert len(payload["inputs"]["sources"]["sha256"]) == 64
    assert main(["--similarity-threshold", "1", "--overwrite", "--output", str(report)]) == 0
    assert json.loads(report.read_text())["overall"]["matched"] == 1


def test_missing_result_is_a_dataset_error_without_a_partial_report(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    sample = SAMPLE.with_name("sample_predictions.jsonl")
    path.write_text(sample.read_text().splitlines()[0] + "\n", encoding="utf-8")
    report = tmp_path / "report.json"
    assert main(["--predictions", str(path), "--output", str(report)]) == 1
    assert not report.exists()
