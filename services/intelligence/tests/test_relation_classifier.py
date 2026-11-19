"""Exercise classification, dual quote verification, and evidence scoring offline."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.relations.candidates import ChunkPair
from app.relations.classifier import ClassificationError, RelationClassifier
from intellidocs_intelligence.cache import DiskCache
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.models import Chunk, OrgConfig

REPLACEMENT = "This directive supersedes Order 12 in its entirety."
OLD_ORDER = "The contractor must send a paper copy within ten days."


@pytest.fixture
def org() -> OrgConfig:
    path = Path(__file__).resolve().parents[3] / "config/orgs/example.yaml"
    return OrgConfig.model_validate_json(path.read_text(encoding="utf-8"))


def pair(
    source: str = REPLACEMENT,
    target: str = OLD_ORDER,
    *,
    language: str = "en",
    explicit: bool = True,
    target_clause: str | None = None,
) -> ChunkPair:
    return ChunkPair(
        Chunk(
            id="new-chunk",
            doc_id="new-doc",
            page=1,
            char_start=5,
            char_end=5 + len(source),
            text=source,
            org_id="example",
            language=language,
            doc_date=date(2026, 6, 1),
        ),
        Chunk(
            id="old-chunk",
            doc_id="old-doc",
            page=3,
            char_start=20,
            char_end=20 + len(target),
            text=target,
            org_id="example",
            language=language,
            doc_date=date(2025, 6, 1),
            clause_number=target_clause,
        ),
        ("explicit_reference",) if explicit else ("similarity",),
        ("number:12",) if explicit else (),
        False,
        None,
    )


def response(candidate: ChunkPair, kind: str = "supersedes", scope: str = "whole_document") -> dict:
    return {
        "relation_type": kind,
        "scope": scope,
        "source_quote": candidate.source_chunk.text,
        "target_quote": candidate.target_chunk.text,
    }


def test_explicit_supersession_has_two_citations_and_full_evidence_score(org: OrgConfig) -> None:
    candidate = pair()
    provider = FakeProvider("fixture", response(candidate))
    relation = RelationClassifier(provider, org).classify(candidate)
    assert relation is not None
    assert relation.relation_type == "supersedes" and relation.scope == "whole_document"
    assert relation.status == "proposed" and relation.needs_review is False
    assert [(c.doc_id, c.char_start, c.quote) for c in relation.source_citations] == [
        ("new-doc", 5, REPLACEMENT)
    ]
    assert [(c.doc_id, c.page, c.char_start, c.quote) for c in relation.target_citations] == [
        ("old-doc", 3, 20, OLD_ORDER)
    ]
    assert relation.confidence_breakdown is not None
    assert sum(relation.confidence_breakdown.model_dump().values()) == pytest.approx(1)
    assert relation.confidence == 1


def test_partial_amendment_is_limited_to_the_referenced_clause(org: OrgConfig) -> None:
    candidate = pair(
        "Clause 4.2 of Order 12 is amended to require quarterly reports.",
        "4.2 The supplier must submit monthly reports.",
        target_clause="4.2",
    )
    relation = RelationClassifier(FakeProvider("fixture", response(candidate, "amends")), org)
    result = relation.classify(candidate)
    assert result is not None and result.scope == "clauses"
    assert result.relation_type == "amends" and result.target_citations[0].clause_number == "4.2"


@pytest.mark.parametrize("bad_side", ["source_quote", "target_quote"])
def test_altered_quote_is_rejected_and_proposal_requires_review(
    org: OrgConfig, bad_side: str
) -> None:
    candidate = pair()
    draft = response(candidate)
    original = draft[bad_side]
    draft[bad_side] = original.replace("12", "13").replace("ten", "two")
    result = RelationClassifier(FakeProvider("fixture", draft), org).classify(candidate)
    assert result is not None and result.status == "proposed" and result.needs_review is True
    citations = result.source_citations if bad_side == "source_quote" else result.target_citations
    assert citations == [] and draft[bad_side] not in result.model_dump_json()
    assert len(result.source_citations) + len(result.target_citations) == 1
    assert result.confidence_breakdown and result.confidence_breakdown.both_quotes_verified == 0


def test_conflict_requires_different_quotes_and_is_distinct_from_supersession(
    org: OrgConfig,
) -> None:
    monthly = "While in force, suppliers must file monthly reports."
    quarterly = "While in force, suppliers must file quarterly reports."
    conflict = pair(monthly, quarterly, explicit=False)
    provider = FakeProvider("fixture", response(conflict, "conflicts_with"))
    classifier = RelationClassifier(provider, org)
    result = classifier.classify(conflict)
    assert result is not None and result.relation_type == "conflicts_with"
    assert result.status == "proposed" and result.needs_review is True
    assert result.confidence_breakdown and result.confidence_breakdown.recognized_cue > 0
    # Quoting only the directive cannot turn an explicit replacement into a conflict.
    replacement = pair(f"{REPLACEMENT} {monthly}", quarterly)
    provider.response = {**response(replacement, "conflicts_with"), "source_quote": monthly}
    assert classifier.classify(replacement) is None
    identical = pair(monthly, monthly)
    provider.response = response(identical, "conflicts_with")
    assert classifier.classify(identical) is None


def test_unstated_change_requires_review(org: OrgConfig) -> None:
    candidate = pair("Suppliers must file every month.", OLD_ORDER, explicit=False)
    result = RelationClassifier(FakeProvider("fixture", response(candidate)), org).classify(
        candidate
    )
    assert result is not None and result.needs_review and result.status == "proposed"
    assert result.confidence_breakdown and result.confidence_breakdown.recognized_cue == 0


def test_change_without_target_link_or_with_negated_language_requires_review(
    org: OrgConfig,
) -> None:
    similar = pair(explicit=False)
    provider = FakeProvider("fixture", response(similar))
    classifier = RelationClassifier(provider, org)
    similar_result = classifier.classify(similar)
    assert similar_result is not None and similar_result.needs_review is True
    negated = pair("Nothing in this directive supersedes Order 12.")
    provider.response = {**response(negated), "source_quote": "supersedes Order 12."}
    negated_result = classifier.classify(negated)
    assert negated_result is not None and negated_result.needs_review is True
    assert negated_result.confidence_breakdown
    assert negated_result.confidence_breakdown.recognized_cue == 0


def test_hindi_change_cue_and_older_date_are_grounded(org: OrgConfig) -> None:
    candidate = pair(
        "यह आदेश परिपत्र संख्या १२ का अधिक्रमण करता है।",
        "आपूर्तिकर्ता को हर महीने रिपोर्ट देनी होगी।",
        language="hi",
    )
    classifier = RelationClassifier(FakeProvider("fixture", response(candidate)), org)
    result = classifier.classify(candidate)
    assert result is not None and result.needs_review is False
    assert result.source_citations[0].quote == candidate.source_chunk.text
    changed = replace(
        candidate,
        target_chunk=candidate.target_chunk.model_copy(update={"doc_date": date(2027, 1, 1)}),
    )
    inconsistent = classifier.classify(changed)
    assert inconsistent is not None and inconsistent.needs_review is True
    assert inconsistent.confidence < result.confidence
    assert (
        inconsistent.confidence_breakdown
        and inconsistent.confidence_breakdown.date_order_consistent == 0
    )


class RetryProvider(FakeProvider):
    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        context = json.loads(prompt.split("\nPAIR_JSON\n", 1)[1])
        assert set(context) == {"NEW", "OLD"}
        assert context["NEW"]["text"] == REPLACEMENT and context["OLD"]["text"] == OLD_ORDER
        assert "confidence" not in schema["properties"]
        if self.calls == 0:
            self.calls += 1
            raise json.JSONDecodeError("Invalid JSON", "", 0)
        return super().generate(prompt, schema)


def test_retry_succeeds_and_caches_only_valid_model_output(tmp_path: Path, org: OrgConfig) -> None:
    candidate = pair()
    provider = RetryProvider("fixture", response(candidate))
    classifier = RelationClassifier(provider, org, cache=DiskCache(tmp_path))
    result = classifier.classify(candidate)
    again = classifier.classify(candidate)
    assert result is not None and again is not None and result.id == again.id
    assert provider.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_exhausted_output_is_not_cached_and_empty_result_is_supported(
    tmp_path: Path, org: OrgConfig
) -> None:
    provider = FakeProvider("fixture", {"scope": "whole_document"})
    classifier = RelationClassifier(provider, org, cache=DiskCache(tmp_path))
    with pytest.raises(ClassificationError):
        classifier.classify(pair())
    assert provider.calls == 2 and not list(tmp_path.glob("*.json"))
    provider.response = {
        "relation_type": None,
        "scope": None,
        "source_quote": "",
        "target_quote": "",
    }
    assert classifier.classify(pair()) is None and provider.calls == 3
