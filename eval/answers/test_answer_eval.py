"""Hand-checked gold metrics and paired offline answer evaluation."""

import json
import socket
from pathlib import Path

import pytest
from answer_metrics import GoldAnswer, ScoredClaim, baseline_claims, score, summarize
from evaluate_answers import SAMPLE, load_gold, main, read_jsonl

from app.answering import AnswerDraft
from app.generation import GeneratedClaim
from app.verification import CitationVerifier, VerificationConfig
from intellidocs_intelligence.models import Chunk


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Answer evaluation tests must be offline")

    monkeypatch.setattr(socket.socket, "connect", fail)
    for name, value in {
        "EMBEDDING_BACKEND": "fake",
        "SPARSE_EMBEDDING_BACKEND": "native",
        "RERANKER_BACKEND": "fake",
        "VERIFICATION_SUPPORT_CHECK": "false",
        "VERIFICATION_FUZZY_THRESHOLD": "1.0",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("STORAGE_DIR", raising=False)


def evidence() -> AnswerDraft:
    text = "The filing fee is waived."
    return AnswerDraft(
        [Chunk(id="fee", doc_id="fees", page=3, char_start=0, char_end=len(text), text=text)],
        [GeneratedClaim(text=text, chunk_id="fee", quote=text)],
        None,
    )


def test_gold_scoring_counts_bad_citations_wrong_pages_and_aliases() -> None:
    gold, _ = load_gold(SAMPLE)
    fees = gold[-1]
    draft = evidence()
    [valid] = baseline_claims(draft)
    counts = score(fees, [valid, ScoredClaim("Unsupported details.", [None])], draft)
    metrics = summarize([counts])
    assert metrics["citation_precision"] == 0.5
    assert metrics["unsupported_claim_rate"] == 0.5
    assert metrics["answer_coverage"] == 1
    assert metrics["abstention_rate_unanswerable"] is None
    assert (
        score(fees, [ScoredClaim("No filing fee is charged.", valid.citations)], draft)[
            "covered_points"
        ]
        == 1
    )
    # Duplicate claims/citations must not inflate point coverage.
    assert score(fees, [valid, valid], draft)["covered_points"] == 1
    wrong_page = fees.model_copy(
        update={"relevant": [fees.relevant[0].model_copy(update={"page": 4})]}
    )
    assert score(wrong_page, [valid], draft)["correct_citations"] == 0
    citation = valid.citations[0]
    assert citation is not None
    corrupt = citation.model_copy(update={"char_start": 1})
    assert score(fees, [ScoredClaim(valid.text, [corrupt])], draft)["unsupported_claims"] == 1
    # Matching a point by text alone, without source evidence, never covers it.
    assert score(fees, [ScoredClaim(valid.text, [])], draft)["covered_points"] == 0


def test_metrics_do_not_trust_the_verifiers_own_status() -> None:
    gold, _ = load_gold(SAMPLE)
    draft = evidence()
    draft.claims[0].text = "The filing fee is $100."
    verified = CitationVerifier(VerificationConfig()).build_answer(
        draft.claims, draft.chunks, model="fixture"
    )
    # Source attribution succeeds, but the independently labeled answer is wrong.
    assert verified.claims[0].status == "verified"
    counts = score(
        gold[-1], [ScoredClaim(claim.text, claim.citations) for claim in verified.claims], draft
    )
    assert counts["unsupported_claims"] == 1 and counts["covered_points"] == 0


def test_empty_outputs_have_undefined_precision_and_unanswerable_abstention() -> None:
    gold, _ = load_gold(SAMPLE)
    counts = score(gold[2], [], evidence())
    metrics = summarize([counts])
    assert metrics["abstention_rate_unanswerable"] == 1
    assert metrics["citation_precision"] is None
    assert metrics["unsupported_claim_rate"] is None
    assert metrics["answer_coverage"] is None


def test_gold_validation_includes_source_location_and_consistency(tmp_path: Path) -> None:
    path = tmp_path / "gold.jsonl"
    row = {
        "id": "q",
        "query": "fee?",
        "answerable": True,
        "expected_answer_points": [],
        "relevant": [],
    }
    path.write_text("\n" + json.dumps(row) + "\n")
    with pytest.raises(ValueError, match=r"gold\.jsonl:2:.*validation error"):
        read_jsonl(path, GoldAnswer)
    row["answerable"] = False
    path.write_text((json.dumps(row) + "\n") * 2)
    with pytest.raises(ValueError, match="duplicate query IDs"):
        load_gold(path)


def test_sample_compares_same_drafts_and_preserves_rates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = SAMPLE.with_name("sample_responses.json")
    output = tmp_path / "report.json"
    args = ["--responses", str(responses), "--output", str(output)]
    assert main(args) == 0
    report = json.loads(output.read_text())
    baseline, verified = report["runs"]["baseline"], report["runs"]["verified"]
    assert baseline["overall"]["unsupported_claim_rate"] == 0.5
    assert verified["overall"]["unsupported_claim_rate"] == 0
    assert baseline["overall"]["citation_precision"] == 0.5
    assert verified["overall"]["citation_precision"] == 1
    assert baseline["overall"]["abstention_rate_unanswerable"] == 0
    assert verified["overall"]["abstention_rate_unanswerable"] == 1
    assert baseline["overall"]["answer_coverage"] == pytest.approx(2 / 3)
    assert verified["overall"]["answer_coverage"] == pytest.approx(2 / 3)
    assert report["config"]["model"] == "fixture-claims.v1"
    assert report["config"]["fixture_responses_sha256"]
    assert "baseline" in capsys.readouterr().out
    assert main([*args, "--overwrite"]) == 0
    assert json.loads(output.read_text())["runs"] == report["runs"]

    def should_not_verify(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Baseline must not invoke verification")

    monkeypatch.setattr(CitationVerifier, "build_answer", should_not_verify)
    assert main([*args, "--mode", "baseline", "--overwrite"]) == 0
    assert list(json.loads(output.read_text())["runs"]) == ["baseline"]
