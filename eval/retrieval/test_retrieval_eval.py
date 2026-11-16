"""Metric hand checks and a reproducible end-to-end sample run."""

import json
import socket
from math import log2
from pathlib import Path

import pytest
from evaluate_retrieval import CONFIGURATIONS, SAMPLE, load_gold, main, read_jsonl
from intellidocs_intelligence.models import Chunk
from metrics import GoldQuery, Target, score_ranking


def chunk(doc_id: str, **fields: object) -> Chunk:
    return Chunk.model_validate(
        {
            "id": doc_id,
            "doc_id": doc_id,
            "text": "report",
            "char_start": 0,
            "char_end": 6,
            "page": 1,
            **fields,
        }
    )


def test_unique_targets_and_discounts() -> None:
    targets = [Target(doc_id="a", page=2, clause_number="3"), Target(doc_id="b", clause_number="1")]
    relevant = chunk("a", page_start=1, page_end=2, clause_number="3")
    ranking = [
        chunk("a", page=2, clause_number="8"),
        relevant,
        relevant.model_copy(update={"id": "overlap"}),
        chunk("c", clause_number="1"),
        chunk("a", page=1, clause_number="3"),
        chunk("b", clause_number="1"),
    ]
    assert score_ranking(ranking, targets) == pytest.approx(
        {
            "recall_at_5": 0.5,
            "recall_at_10": 1.0,
            "mrr": 0.5,
            "ndcg_at_10": (1 / log2(3) + 1 / log2(7)) / (1 + 1 / log2(3)),
        }
    )


def test_mrr_uses_full_pool_and_empty_ranking_counts_as_zero() -> None:
    targets = [Target(doc_id="match", page=1)]
    late = score_ranking([chunk("other")] * 10 + [chunk("match")], targets)
    assert late == {"recall_at_5": 0.0, "recall_at_10": 0.0, "mrr": 1 / 11, "ndcg_at_10": 0.0}
    assert all(metric == 0.0 for metric in score_ranking([], targets).values())


def test_gold_validation_reports_input_location(tmp_path: Path) -> None:
    source = tmp_path / "invalid.jsonl"
    query = {
        "id": "q",
        "language": "en",
        "query_type": "cross_lingual",
        "query": "where?",
        "targets": [{"doc_id": "a"}],
    }
    source.write_text("\n" + json.dumps(query) + "\n")
    with pytest.raises(ValueError, match=r"invalid\.jsonl:2:.*1 validation error"):
        read_jsonl(source, GoldQuery)
    query["targets"] = [{"doc_id": "a", "page": 1}]
    query["filters"] = {"doc_date_to": "2026-02-30"}
    source.write_text(json.dumps(query) + "\n")
    with pytest.raises(ValueError, match="filters for query q.*calendar date"):
        load_gold(source)


def test_sample_reports_all_five_runs_and_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def offline(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Sample evaluation must be offline")

    monkeypatch.setattr(socket.socket, "connect", offline)
    for name, value in {
        "EMBEDDING_BACKEND": "fake",
        "EMBEDDING_BATCH_SIZE": "8",
        "SPARSE_EMBEDDING_BACKEND": "native",
        "RERANKER_BACKEND": "fake",
        "RETRIEVAL_CANDIDATE_POOL_SIZE": "12",
        "RETRIEVAL_RRF_K": "2",
    }.items():
        monkeypatch.setenv(name, value)
    output = tmp_path / "report.json"
    assert main(["--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["config"]["seed"] == 42
    assert report["gold"]["queries"] == 5
    assert tuple(report["runs"]) == tuple(spec.name for spec in CONFIGURATIONS)
    gold, _ = load_gold(SAMPLE)
    for spec in CONFIGURATIONS:
        run = report["runs"][spec.name]
        assert run["config"]["retrieval"]["candidate_pool_size"] == 12
        assert run["config"]["retrieval"]["mode"] == spec.mode
        assert (run["config"]["reranker"] == "FakeReranker") == spec.rerank
        assert run["overall"]["queries"] == 5
        assert {lang: row["queries"] for lang, row in run["by_language"].items()} == {
            "en": 3,
            "hi": 2,
        }
        assert {kind: row["queries"] for kind, row in run["by_query_type"].items()} == {
            "cross_lingual": 2,
            "same_language": 3,
        }
        assert [row["filters"] for row in run["per_query"]] == [
            query.filters if spec.filters else None for query in gold
        ]
    assert "Recall@10" in capsys.readouterr().out
    assert main(["--output", str(output), "--overwrite"]) == 0
    repeated = json.loads(output.read_text())
    assert repeated["runs"] == report["runs"]
