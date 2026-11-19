"""Check obligation citations, conservative resolution, and deduplication."""

from datetime import date
from pathlib import Path

import pytest
from test_api import validate_contract

from app.chunking import Chunker, ChunkingConfig, canonical_document
from app.extraction import DraftObligation, ObligationProcessor
from app.extraction.confidence import ConfidenceWeights
from app.ingestion import load_org
from intellidocs_intelligence.models import (
    Chunk,
    ObligationDeadline,
    ObligationOwner,
    OrgConfig,
    Page,
    ParsedDocument,
    TextBlock,
)


@pytest.fixture
def org(monkeypatch: pytest.MonkeyPatch) -> OrgConfig:
    monkeypatch.setenv("ORG_CONFIG_DIR", str(Path(__file__).resolve().parents[3] / "config/orgs"))
    monkeypatch.delenv("EXTRACTION_CONFIDENCE_WEIGHTS", raising=False)
    monkeypatch.delenv("EXTRACTION_FUZZY_THRESHOLD", raising=False)
    return load_org("example")


def sources(
    texts: list[str],
    doc_date: date | None = None,
    language: str = "en",
) -> tuple[ParsedDocument, list[Chunk]]:
    document = ParsedDocument(
        id="policy",
        filename="policy.pdf",
        mime_type="application/pdf",
        source_uri="file:///policy.pdf",
        status="ready",
        doc_date=doc_date,
        pages=[
            Page(
                page=number,
                language=language,
                blocks=[
                    TextBlock(
                        type="heading",
                        text=f"Section {number}",
                        bbox=[0, 0, 10, 10],
                        reading_order=0,
                        heading_level=1,
                    ),
                    TextBlock(
                        type="clause",
                        text=text,
                        bbox=[0, 10, 10, 20],
                        reading_order=1,
                        clause_number=str(number),
                    ),
                ],
            )
            for number, text in enumerate(texts, 1)
        ],
    )
    return document, [
        chunk
        for chunk in Chunker(ChunkingConfig(100, 0, "whitespace")).chunk(document)
        if chunk.clause_number is not None
    ]


def draft(chunk: Chunk, **changes: str | None) -> DraftObligation:
    return DraftObligation.model_validate(
        {
            "chunk_id": chunk.chunk_id,
            "action": "File the report",
            "owner_text": "Finance Officer",
            "deadline_text": "by 5 December 2026",
            "condition": None,
            "obligation_type": "reporting",
            "quote": chunk.text,
            **changes,
        }
    )


def test_exact_quote_verifies_offsets_alias_and_absolute_deadline(org: OrgConfig) -> None:
    text = "The FINANCE-OFFICER must file the report by 5 December 2026."
    document, [chunk] = sources([text])
    result = ObligationProcessor(org).process(
        document, [chunk], [draft(chunk, owner_text="FINANCE-OFFICER")]
    )
    [obligation] = result.obligations
    assert result.stats.verified == 1 and obligation.status == "verified"
    assert isinstance(obligation.owner, ObligationOwner)
    assert obligation.owner.resolved_department == "finance"
    assert obligation.owner.resolved_role == "finance_officer"
    assert obligation.owner.raw_text == "FINANCE-OFFICER"
    assert isinstance(obligation.deadline, ObligationDeadline)
    assert obligation.deadline.normalized_date == date(2026, 12, 5)
    assert obligation.deadline.anchor_date is None
    assert obligation.citations is not None
    [citation] = obligation.citations
    assert citation.page_start == citation.page_end == 1
    assert citation.clause_number == "1"
    assert canonical_document(document).text[citation.char_start : citation.char_end] == text
    assert org.routing_policy is not None
    assert obligation.confidence > org.routing_policy.min_confidence
    breakdown = obligation.confidence_breakdown
    assert breakdown is not None
    assert breakdown.model_dump() == {
        "quote_exact": 0.25,
        "quote_fuzzy": 0,
        "owner_resolved": 0.55,
        "deadline_resolved": 0.1,
        "recognized_cue": 0.05,
        "cross_chunk_agreement": 0,
    }
    assert sum(breakdown.model_dump().values()) == pytest.approx(obligation.confidence)
    validate_contract(result.model_dump(mode="json"), "extraction-result.v1.schema.json")


def test_altered_quote_rejected_even_with_valid_owner_and_deadline(org: OrgConfig) -> None:
    text = "Finance Officer must file the report by 5 December 2026."
    document, [chunk] = sources([text])
    result = ObligationProcessor(org).process(
        document,
        [chunk],
        [
            draft(chunk, quote=text.replace("5 December", "6 December")),
            draft(chunk),
        ],
    )
    rejected, accepted = result.obligations
    assert rejected.status == "rejected" and rejected.citations == []
    assert rejected.confidence == 0
    assert isinstance(rejected.owner, ObligationOwner) and not rejected.owner.resolved
    assert isinstance(rejected.deadline, ObligationDeadline) and not rejected.deadline.resolved
    assert result.stats.rejected == result.stats.verified == 1
    assert accepted.status == "verified"


def test_fuzzy_quote_earns_less_and_uses_the_canonical_source(
    org: OrgConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXTRACTION_FUZZY_THRESHOLD", "0.95")
    text = "Finance Officer must file the report by 5 December 2026."
    document, [chunk] = sources([text])
    fuzzy_draft = draft(chunk, quote=text.replace("report", "reprot"))
    processor = ObligationProcessor(org)
    [fuzzy] = processor.process(document, [chunk], [fuzzy_draft]).obligations
    [exact] = processor.process(document, [chunk], [draft(chunk)]).obligations
    [both] = processor.process(document, [chunk], [fuzzy_draft, draft(chunk)]).obligations
    assert fuzzy.status == exact.status == "verified"
    assert fuzzy.citations and fuzzy.citations[0].quote == text
    assert fuzzy.confidence < exact.confidence
    assert fuzzy.confidence_breakdown is not None
    assert fuzzy.confidence_breakdown.quote_exact == 0
    assert fuzzy.confidence_breakdown.quote_fuzzy == 0.12
    assert both.confidence_breakdown == exact.confidence_breakdown


def test_cue_must_appear_in_the_quoted_span(org: OrgConfig) -> None:
    quote = "Finance Officer files the report by 5 December 2026."
    document, [chunk] = sources(["The board must review every draft. " + quote])
    [obligation] = (
        ObligationProcessor(org).process(document, [chunk], [draft(chunk, quote=quote)]).obligations
    )
    assert obligation.confidence_breakdown is not None
    assert obligation.confidence_breakdown.quote_exact > 0
    assert obligation.confidence_breakdown.recognized_cue == 0


def test_weights_are_normalized_and_validate_exact_priority() -> None:
    env = {"EXTRACTION_CONFIDENCE_WEIGHTS": '{"quote_exact": 0.5}'}
    weights = ConfidenceWeights.from_env(env)
    assert weights.contributions(
        quote_exact=True,
        quote_fuzzy=False,
        owner_resolved=False,
        deadline_resolved=False,
        recognized_cue=False,
        cross_chunk_agreement=False,
    ).quote_exact == pytest.approx(0.4)
    with pytest.raises(ValueError, match="higher weight"):
        ConfidenceWeights.from_env({"EXTRACTION_CONFIDENCE_WEIGHTS": '{"quote_exact": 0}'})


@pytest.mark.parametrize("anchor", [None, date(2026, 11, 15)])
def test_relative_deadline_uses_only_document_anchor(org: OrgConfig, anchor: date | None) -> None:
    raw = "within 30 days of receipt"
    document, [chunk] = sources([f"Finance Officer must file the report {raw}."], anchor)
    [obligation] = (
        ObligationProcessor(org)
        .process(
            document,
            [chunk],
            [draft(chunk, deadline_text=raw)],
        )
        .obligations
    )
    assert isinstance(obligation.deadline, ObligationDeadline)
    assert obligation.deadline.raw_text == raw and obligation.deadline.is_relative
    assert obligation.deadline.anchor_date == anchor
    if anchor is None:
        assert obligation.status == "needs_review"
        assert not obligation.deadline.resolved and obligation.deadline.normalized_date is None
    else:
        assert obligation.status == "verified"
        assert obligation.deadline.resolved
        assert obligation.deadline.normalized_date == date(2026, 12, 15)


@pytest.mark.parametrize(
    "raw", ["5 December", "01/02/2026", "by 2026-02-30", "within 5 business days"]
)
def test_missing_ambiguous_or_unsupported_date_stays_unresolved(org: OrgConfig, raw: str) -> None:
    document, [chunk] = sources(
        [f"Finance Officer must file the report {raw}."], date(2026, 11, 15)
    )
    [obligation] = (
        ObligationProcessor(org)
        .process(
            document,
            [chunk],
            [draft(chunk, deadline_text=raw)],
        )
        .obligations
    )
    assert obligation.status == "needs_review"
    assert isinstance(obligation.deadline, ObligationDeadline)
    assert not obligation.deadline.resolved and obligation.deadline.normalized_date is None


def test_unknown_ambiguous_and_unsourced_owners_do_not_resolve(org: OrgConfig) -> None:
    org.department_aliases["legal"].append("Finance Department")
    document, chunks = sources(
        [
            "External Supplier must file the report by 5 December 2026.",
            "Finance Department must file the report by 5 December 2026.",
            "The report must be filed by 6 December 2026.",
        ]
    )
    result = ObligationProcessor(org).process(
        document,
        chunks,
        [
            draft(chunk, owner_text=owner)
            for chunk, owner in zip(
                chunks,
                ["External Supplier", "Finance Department", "Finance Officer"],
                strict=True,
            )
        ],
    )
    assert result.stats.needs_review == 3
    assert org.routing_policy is not None
    for obligation in result.obligations:
        assert isinstance(obligation.owner, ObligationOwner) and not obligation.owner.resolved
        assert obligation.citations and obligation.owner.resolved_department is None
        assert obligation.confidence < org.routing_policy.min_confidence
        assert obligation.confidence_breakdown is not None
        assert obligation.confidence_breakdown.owner_resolved == 0
    assert isinstance(result.obligations[-1].deadline, ObligationDeadline)
    assert not result.obligations[-1].deadline.resolved


def test_dedup_merges_citations_for_equivalent_owner_and_date(org: OrgConfig) -> None:
    document, chunks = sources(
        [
            "Finance Dept. must file the report by 5 December 2026.",
            "FINANCE must file the report by 2026-12-05.",
        ]
    )
    first = draft(chunks[0], owner_text="Finance Dept.")
    second = draft(
        chunks[1], action="file the report", owner_text="FINANCE", deadline_text="by 2026-12-05"
    )
    result = ObligationProcessor(org).process(document, chunks, [first, second, first])
    [obligation] = result.obligations
    assert result.stats.total == result.stats.verified == 1
    assert obligation.citations is not None
    assert [(citation.page, citation.quote) for citation in obligation.citations] == [
        (1, chunks[0].text),
        (2, chunks[1].text),
    ]
    assert obligation.confidence_breakdown is not None
    assert obligation.confidence_breakdown.cross_chunk_agreement > 0
    assert obligation.confidence == 1


def test_repetition_over_the_same_source_span_does_not_raise_confidence(org: OrgConfig) -> None:
    text = "Finance Officer must file the report by 5 December 2026."
    document, [chunk] = sources([text])
    overlap = chunk.model_copy(update={"chunk_id": "overlap"})
    [obligation] = (
        ObligationProcessor(org)
        .process(document, [chunk, overlap], [draft(chunk), draft(overlap), draft(chunk)])
        .obligations
    )
    assert obligation.confidence_breakdown is not None
    assert obligation.confidence_breakdown.cross_chunk_agreement == 0


@pytest.mark.parametrize(
    ("raw_date", "expected"),
    [
        ("५ दिसंबर २०२६ तक", date(2026, 12, 5)),
        ("प्राप्ति के बाद ३० दिनों के भीतर", date(2026, 12, 15)),
    ],
)
def test_hindi_owner_unicode_and_deadline(org: OrgConfig, raw_date: str, expected: date) -> None:
    owner = "क़ानूनी विभाग"  # The configured alias uses the precomposed nukta character.
    document, [chunk] = sources(
        [f"{owner} को {raw_date} रिपोर्ट जमा करनी होगी।"], date(2026, 11, 15), "hi"
    )
    [obligation] = (
        ObligationProcessor(org)
        .process(
            document,
            [chunk],
            [draft(chunk, action="रिपोर्ट जमा करें", owner_text=owner, deadline_text=raw_date)],
        )
        .obligations
    )
    assert obligation.status == "verified"
    assert isinstance(obligation.owner, ObligationOwner)
    assert obligation.owner.resolved_department == "legal"
    assert isinstance(obligation.deadline, ObligationDeadline)
    assert obligation.deadline.normalized_date == expected
