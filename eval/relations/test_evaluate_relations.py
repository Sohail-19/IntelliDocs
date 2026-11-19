from pathlib import Path

import pytest
from evaluate_relations import GoldPair, evaluate, read_jsonl
from intellidocs_intelligence.models import ParseResult, RelationResult

FIXTURES = Path(__file__).parent


def inputs() -> tuple[list[GoldPair], list[RelationResult], list[ParseResult]]:
    return (
        read_jsonl(FIXTURES / "sample.jsonl", GoldPair)[0],
        read_jsonl(FIXTURES / "sample_predictions.jsonl", RelationResult)[0],
        read_jsonl(FIXTURES / "sample_sources.jsonl", ParseResult)[0],
    )


def test_measures_classification_authority_and_evidence_separately() -> None:
    report = evaluate(*inputs())
    by_kind = report["by_relation_type"]
    assert isinstance(by_kind, dict)
    assert by_kind["supersedes"] == {
        "tp": 1,
        "fp": 1,
        "fn": 0,
        "precision": 0.5,
        "recall": 1,
        "f1": 2 / 3,
    }
    assert by_kind["references"]["fn"] == 1
    assert report["false_supersession"] == {
        "rate": 0.25,
        "eligible_documents": 4,
        "false_documents": ["unrelated-old"],
    }
    assert report["confirmed_false_supersession"] == {
        "rate": 0,
        "eligible_documents": 4,
        "false_documents": [],
    }
    assert report["citation_faithfulness"] == 0.9
    assert report["gold_evidence_page_coverage"] == 0.8
    failures = report["failed_citations"]
    assert isinstance(failures, list)
    assert [failure["relation_id"] for failure in failures] == ["relation-unrelated"]


def test_deduplicates_classification_and_requires_faithful_evidence_on_gold_pages() -> (
    None
):
    gold, predictions, sources = inputs()
    result = predictions[0]
    first = result.relations[0]
    citation = first.source_citations[0].model_copy(update={"page": 2})
    wrong_page = first.model_copy(update={"source_citations": [citation]})
    result.relations = [
        wrong_page,
        wrong_page.model_copy(update={"id": "duplicate-amendment"}),
    ]
    report = evaluate(gold, predictions, sources, confirmed_only=True)
    by_kind = report["by_relation_type"]
    assert isinstance(by_kind, dict)
    assert by_kind["amends"]["tp"] == 1
    assert report["citations"] == 4
    assert report["citation_faithfulness"] == 0.5
    assert report["gold_evidence_page_coverage"] == 0


def test_rejects_unlabeled_pairs_and_missing_empty_results() -> None:
    gold, predictions, sources = inputs()
    different_pair = (
        predictions[0]
        .relations[0]
        .model_copy(update={"target_doc_id": "unrelated-old"})
    )
    predictions[0].relations = [different_pair]
    with pytest.raises(ValueError, match="Missing gold label for predicted pair"):
        evaluate(gold, predictions, sources)
    with pytest.raises(ValueError, match="exactly one RelationResult"):
        evaluate(gold, predictions[:-1], sources)
