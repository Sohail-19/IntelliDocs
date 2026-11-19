"""Source-backed citations, Unicode offsets, and fail-closed answer display."""

import json
import socket
from pathlib import Path

import pytest
from test_api import validate_contract

from app.chunking import Chunker, ChunkingConfig, canonical_document
from app.generation import GeneratedClaim, GenerationResult
from app.verification import CitationVerifier, VerificationConfig, verifier_from_env
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.models import Chunk, Page, ParsedDocument, TextBlock


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Verification tests must not access a network")

    monkeypatch.setattr(socket.socket, "connect", fail)


def chunk(text: str = "The report must be submitted by Friday.") -> Chunk:
    return Chunk(
        id="old-id",
        chunk_id="chunk-1",
        doc_id="doc-1",
        page=3,
        page_start=3,
        page_end=3,
        text=text,
        char_start=10,
        char_end=10 + len(text),
        clause_number="4.2",
        heading_path=["Section 4", "4.2"],
    )


def claim(quote: str = "must be submitted by Friday.", **fields: str) -> GeneratedClaim:
    return GeneratedClaim(
        **{"text": "Submission is due Friday.", "chunk_id": "chunk-1", "quote": quote, **fields}
    )


def test_exact_quote_cites_only_matching_span() -> None:
    source = chunk()
    [result] = CitationVerifier().verify([claim()], [source])
    assert result.status == "verified"
    [citation] = result.citations
    assert citation.char_start == source.char_start + source.text.index(claim().quote)
    assert citation.char_end == source.char_end
    assert citation.quote == claim().quote
    assert citation.doc_id == source.doc_id
    assert (citation.page, citation.page_start, citation.page_end) == (3, 3, 3)
    assert citation.clause_number == source.clause_number
    assert citation.heading_path == source.heading_path
    full_text = " " * source.char_start + source.text
    assert full_text[citation.char_start : citation.char_end] == citation.quote
    validate_contract(
        citation.model_dump(mode="json", exclude_none=True), "citation.v1.schema.json"
    )


@pytest.mark.parametrize(
    "draft",
    [
        claim("must not be submitted by Friday."),
        claim("must be submitted by Monday."),
        claim(chunk_id="not-retrieved"),
        claim(" \n\t "),
    ],
)
def test_altered_quote_wrong_id_and_empty_evidence_fail(draft: GeneratedClaim) -> None:
    [result] = CitationVerifier().verify([draft], [chunk()])
    assert result.status == "unverified" and result.citations == []


@pytest.mark.parametrize(
    "original,quote",
    [
        ("क़ानून में शुल्क देय है।", "क़ानून में शुल्क देय है।"),
        ("क़ानून में शुल्क देय है।", "क़ानून में शुल्क देय है।"),
        ("प्रस्तावना: क़ानून\n\t में  शुल्क देय है। समाप्त।", "क़ानून में शुल्क देय है।"),
        ("Before e\u0301\u0323 after.", "e\u0323\u0301"),
    ],
)
def test_unicode_and_whitespace_fold_back_to_raw_source(original: str, quote: str) -> None:
    source = chunk(original)
    [result] = CitationVerifier().verify([claim(quote)], [source])
    assert result.status == "verified"
    [citation] = result.citations
    document_text = " " * source.char_start + original
    assert document_text[citation.char_start : citation.char_end] == citation.quote
    assert citation.quote in original


def test_cannot_match_inside_a_decomposed_character() -> None:
    [result] = CitationVerifier().verify([claim("क")], [chunk("क़")])
    assert result.status == "unverified"


def test_fuzzy_fallback_is_configurable_and_returns_source_quote() -> None:
    source = chunk()
    draft = claim("must be submited by Friday.")
    assert CitationVerifier().verify([draft], [source])[0].status == "unverified"
    [result] = CitationVerifier(VerificationConfig(fuzzy_threshold=0.95)).verify([draft], [source])
    assert result.status == "verified"
    assert result.citations[0].quote == "must be submitted by Friday."
    [rejected] = CitationVerifier(VerificationConfig(fuzzy_threshold=0.95)).verify(
        [claim("Tax exemptions apply to every payment.")], [source]
    )
    assert rejected.status == "unverified"


def two_page_document() -> ParsedDocument:
    def page(number: int, text: str) -> Page:
        return Page(
            page=number,
            language="en",
            blocks=[TextBlock(type="paragraph", text=text, bbox=[0, 0, 10, 10], reading_order=0)],
        )

    return ParsedDocument(
        id="doc-1",
        filename="original.pdf",
        mime_type="application/pdf",
        source_uri="file:///original.pdf",
        status="ready",
        pages=[page(3, "The report must be submitted."), page(4, "Submission is due Friday.")],
    )


@pytest.mark.parametrize("cross_page", [False, True])
def test_multi_page_quotes_use_canonical_block_spans(cross_page: bool) -> None:
    document = two_page_document()
    chunks = Chunker(ChunkingConfig(100, 0, "whitespace")).chunk(document)
    assert len(chunks) == 1
    text = "must be submitted. Submission is due" if cross_page else "Submission is due Friday."
    draft = claim(text, chunk_id=chunks[0].id)
    verifier = CitationVerifier()
    assert verifier.verify([draft], chunks)[0].status == "unverified"
    [result] = verifier.verify([draft], chunks, documents={document.id: document})
    assert result.status == "verified"
    [citation] = result.citations
    assert (citation.page_start, citation.page_end) == ((3, 4) if cross_page else (4, 4))
    assert citation.page == citation.page_start
    assert citation.boxes is not None
    assert [box.page for box in citation.boxes] == ([3, 4] if cross_page else [4])
    assert all(box.bbox == [0, 0, 10, 10] for box in citation.boxes)
    canonical = canonical_document(document)
    assert canonical.text[citation.char_start : citation.char_end] == citation.quote
    validate_contract(
        citation.model_dump(mode="json", exclude_none=True), "citation.v1.schema.json"
    )


def test_non_pdf_citation_keeps_the_page_and_quote_without_layout() -> None:
    document = two_page_document().model_copy(update={"mime_type": "message/rfc822"})
    chunks = Chunker(ChunkingConfig(100, 0, "whitespace")).chunk(document)
    draft = claim("Submission is due Friday.", chunk_id=chunks[0].id)
    [result] = CitationVerifier().verify([draft], chunks, documents={document.id: document})
    [citation] = result.citations
    assert citation.page == 4 and citation.quote == draft.quote and citation.boxes is None


def test_inconsistent_offsets_ambiguous_ids_and_wrong_documents_fail() -> None:
    source = chunk()
    verifier = CitationVerifier()
    for sources, documents in [
        ([source.model_copy(update={"char_end": 999})], None),
        ([source, source], None),
        ([source], {}),
        ([source], {"doc-1": two_page_document()}),
    ]:
        assert verifier.verify([claim()], sources, documents=documents)[0].status == "unverified"
    legacy = source.model_copy(update={"chunk_id": None})
    assert verifier.verify([claim(chunk_id=legacy.id)], [legacy])[0].status == "verified"


class FailingProvider(FakeProvider):
    def generate(self, prompt: str, schema: dict) -> dict:
        raise TimeoutError("provider unavailable")


def test_support_prompt_contains_only_claim_and_original_matched_quote() -> None:
    class RecordingProvider(FakeProvider):
        def generate(self, prompt: str, schema: dict) -> dict:
            self.prompt = prompt
            return super().generate(prompt, schema)

    source = chunk("Unrelated metadata. The report must be submitted by Friday.")
    provider = RecordingProvider(model="test-model", response={"verdict": "supported"})
    verifier = CitationVerifier(VerificationConfig(0.95, support_check=True), provider)
    draft = claim("must be submited by Friday.")
    assert verifier.verify([draft], [source])[0].status == "verified"
    data = json.loads(provider.prompt.split("REQUEST_JSON\n")[1])
    assert data == {"claim": draft.text, "quote": "must be submitted by Friday."}
    assert "Unrelated metadata" not in provider.prompt


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"verdict": "supported"}, "verified"),
        ({"verdict": "not supported"}, "unverified"),
        ({"verdict": "maybe"}, "unverified"),
        ({}, "unverified"),
        ({"verdict": "supported", "extra": True}, "unverified"),
    ],
)
def test_optional_support_stage_uses_strict_verdict(response: dict, expected: str) -> None:
    provider = FakeProvider(model="test-model", response=response)
    verifier = CitationVerifier(VerificationConfig(support_check=True), provider)
    assert verifier.verify([claim()], [chunk()])[0].status == expected
    assert provider.calls == 1
    assert verifier.verify([claim(chunk_id="wrong")], [chunk()])[0].status == "unverified"
    assert provider.calls == 1  # Never ask the LLM to rescue invalid attribution.


def test_support_disabled_skips_provider_and_failures_cannot_approve() -> None:
    provider = FailingProvider(model="test-model")
    assert CitationVerifier(provider=provider).verify([claim()], [chunk()])[0].status == "verified"
    assert (
        CitationVerifier(VerificationConfig(support_check=True), provider)
        .verify([claim()], [chunk()])[0]
        .status
        == "unverified"
    )
    assert (
        CitationVerifier(VerificationConfig(support_check=True))
        .verify([claim()], [chunk()])[0]
        .status
        == "unverified"
    )


def test_answer_excludes_unverified_claims_and_abstains_when_none_remain() -> None:
    verifier = CitationVerifier()
    drafts = GenerationResult(claims=[claim(), claim("Fabricated quote.", text="Secret fiction.")])
    answer = verifier.build_answer(drafts.claims, [chunk()], model="test-model")
    assert answer.answer_text == claim().text
    assert answer.claims[0].status == "verified" and len(answer.claims) == 1
    assert "Secret fiction" not in answer.model_dump_json()
    assert not answer.abstained and answer.abstain_reason == ""
    assert answer.retrieved_chunk_ids == ["chunk-1"]
    validate_contract(answer.model_dump(mode="json", exclude_none=True), "answer.v1.schema.json")
    for claims in [[], [claim("Fabricated quote.")]]:
        abstained = verifier.build_answer(claims, [chunk()], model="test-model")
        assert abstained.abstained and abstained.abstain_reason
        assert abstained.claims == [] and abstained.answer_text == ""
        validate_contract(
            abstained.model_dump(mode="json", exclude_none=True), "answer.v1.schema.json"
        )
    unsupported = CitationVerifier(
        VerificationConfig(support_check=True),
        FakeProvider(model="test-model", response={"verdict": "not supported"}),
    ).build_answer([claim()], [chunk()], model="test-model")
    assert unsupported.abstained and unsupported.claims == []


def test_environment_support_check_reuses_cache_and_rejects_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERIFICATION_SUPPORT_CHECK", "true")
    monkeypatch.setenv("VERIFICATION_FUZZY_THRESHOLD", "0.97")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    verifier = verifier_from_env()
    assert verifier.config.fuzzy_threshold == 0.97
    assert verifier.provider is not None
    # Configure the factory's fake without a network request.
    fake = verifier.provider.provider
    fake.response = {"verdict": "supported"}
    for _ in range(2):
        assert verifier.verify([claim()], [chunk()])[0].status == "verified"
    assert fake.calls == 1
    [cached] = list(tmp_path.glob("*.json"))
    assert json.loads(cached.read_text()) == {"verdict": "supported"}
    monkeypatch.setenv("VERIFICATION_SUPPORT_CHECK", "false")
    assert verifier_from_env().provider is None
    for settings in [
        {"VERIFICATION_SUPPORT_CHECK": "maybe"},
        {"VERIFICATION_FUZZY_THRESHOLD": "0"},
        {"VERIFICATION_FUZZY_THRESHOLD": "1.1"},
        {"VERIFICATION_FUZZY_THRESHOLD": "nan"},
    ]:
        with pytest.raises(ValueError):
            VerificationConfig.from_env(settings)
