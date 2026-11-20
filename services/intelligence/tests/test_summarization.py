"""Grounded audience summaries with an in-memory index and an offline provider."""

import json
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from test_api import validate_contract

from app.chunking import canonical_document
from app.generation import GenerationConfig
from app.retrieval import HybridRetriever, RetrievalConfig
from app.storage import LocalStorage
from app.summarization import SummaryService
from app.summarization.generator import SummaryGenerator
from app.vector_store import QdrantStore
from app.verification import CitationVerifier, VerificationConfig
from intellidocs_intelligence.cache import DiskCache
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.main import app, get_summary_factory
from intellidocs_intelligence.models import (
    Chunk,
    Citation,
    Obligation,
    OrgConfig,
    ParsedDocument,
    ParseResult,
    SummaryProfile,
    SummaryRequest,
)

ROOT = Path(__file__).resolve().parents[3]
FINANCE = "The invoice fee is forty rupees."
LEGAL = "The agreement requires a termination notice."
DUTY = "Finance must reconcile invoices by 2030-05-06."
OTHER = "The courtyard has two trees."
HINDI = "चालान का शुल्क चालीस रुपये है।"
Snapshot = tuple[LocalStorage, QdrantStore, ParsedDocument, OrgConfig]
REWRITES = {
    FINANCE: "The fee on the invoice is forty rupees.",
    LEGAL: "A termination notice is required under the agreement.",
    DUTY: "Invoices must be reconciled by 2030-05-06.",
}


class PromptProvider(FakeProvider):
    def __init__(self, *, invalid_attempts: int = 0, wrong_quote: bool = False) -> None:
        super().__init__(model="summary-fixture")
        self.invalid_attempts = invalid_attempts
        self.wrong_quote = wrong_quote
        self.requests: list[dict[str, Any]] = []

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        assert "points" in schema["properties"]
        request = json.loads(prompt.split("\nREQUEST_JSON\n", 1)[1])
        self.requests.append(request)
        self.calls += 1
        if self.calls <= self.invalid_attempts:
            return {"points": [{"text": "Missing evidence"}]}
        return {
            "points": [
                {
                    "text": HINDI if request["language"] == "hi" else REWRITES[chunk["text"]],
                    "chunk_id": chunk["chunk_id"],
                    "quote": "The invoice fee is ninety rupees."
                    if self.wrong_quote
                    else chunk["text"],
                }
                for chunk in request["chunks"]
            ]
        }


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Snapshot]:
    org = OrgConfig.model_validate_json((ROOT / "config/orgs/example.yaml").read_text("utf-8"))
    storage = LocalStorage(tmp_path)
    document = ParsedDocument.model_validate(
        {
            "id": str(uuid4()),
            "filename": "contract.pdf",
            "mime_type": "application/pdf",
            "source_uri": "file:///contract.pdf",
            "status": "ready",
            "pages": [
                {
                    "page": page,
                    "language": "en",
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": text,
                            "bbox": [0, 0, 10, 10],
                            "reading_order": 0,
                        }
                    ],
                }
                for page, text in enumerate((FINANCE, LEGAL, DUTY, OTHER), 1)
            ],
        }
    )
    storage.save(document.id, document.filename, b"source-v1")
    storage.save_result(document.id, parsed_result(document))
    source = canonical_document(document)
    chunks = [
        Chunk(
            id=f"chunk-{span.page}",
            doc_id=document.id,
            org_id="example",
            page=span.page,
            char_start=span.start,
            char_end=span.end,
            text=span.block.text,
        )
        for span in source.blocks
    ]
    client = QdrantClient(":memory:")
    try:
        store = QdrantStore(client, "summaries", FakeEmbedder())
        store.upsert_chunks(chunks)
        store.upsert_chunks([chunks[0].model_copy(update={"doc_id": str(uuid4())})])
        yield storage, store, document, org
    finally:
        client.close()


def parsed_result(document: ParsedDocument) -> ParseResult:
    return ParseResult(
        document=document, org_id="example", parser_name="fixture", parser_version="1", warnings=[]
    )


def service(
    snapshot: Snapshot,
    provider: PromptProvider,
    *,
    retries: int = 0,
    verifier: CitationVerifier | None = None,
) -> SummaryService:
    storage, store, _, org = snapshot
    return SummaryService(
        storage,
        HybridRetriever(store, RetrievalConfig()),
        SummaryGenerator(provider, GenerationConfig(retries)),
        verifier or CitationVerifier(VerificationConfig(fuzzy_threshold=0.5)),
        DiskCache(storage.root / "summary-cache"),
        lambda _: org,
    )


def assigned(document: ParsedDocument) -> Obligation:
    span = canonical_document(document).blocks[2]
    citation = Citation(
        doc_id=document.id, page=3, char_start=span.start, char_end=span.end, quote=DUTY
    )
    return Obligation.model_validate(
        {
            "id": "assigned-1",
            "doc_id": document.id,
            "action": "Reconcile vendor invoices",
            "owner": {
                "raw_text": "Finance",
                "resolved_department": "finance",
                "resolved_role": None,
                "resolved": True,
            },
            "deadline": {
                "raw_text": "2030-05-06",
                "normalized_date": "2030-05-06",
                "is_relative": False,
                "anchor_date": None,
                "resolved": True,
            },
            "obligation_type": "duty",
            "citations": [citation],
            "confidence": 1.0,
            "status": "verified",
            "extractor_version": "fixture",
        }
    )


def test_different_departments_receive_different_evidence(setup: Snapshot) -> None:
    _, _, doc, _ = setup
    provider = PromptProvider()
    summarizer = service(setup, provider)
    finance = summarizer.summarize(
        SummaryRequest(doc_id=doc.id, department="finance", language="en"), assigned_obligations=[]
    )
    legal = summarizer.summarize(
        SummaryRequest(doc_id=doc.id, department="legal", language="en"), assigned_obligations=[]
    )
    assert [point.citations[0].quote for point in finance.points] == [FINANCE]
    assert [point.citations[0].quote for point in legal.points] == [LEGAL]
    assert provider.calls == 2
    assert [chunk["text"] for chunk in provider.requests[0]["chunks"]] == [FINANCE]
    assert [chunk["text"] for chunk in provider.requests[1]["chunks"]] == [LEGAL]
    validate_contract(legal.model_dump(mode="json", exclude_none=True), "summary.v1.schema.json")


def test_quote_with_a_changed_amount_does_not_pass_fuzzy_matching(setup: Snapshot) -> None:
    _, _, doc, _ = setup
    provider = PromptProvider(wrong_quote=True)
    result = service(setup, provider).summarize(
        SummaryRequest(doc_id=doc.id, department="finance", language="en"), assigned_obligations=[]
    )
    assert provider.calls == 1
    assert result.not_relevant and result.relevance_reason and not result.points


def test_irrelevant_audience_gets_a_localized_reason_without_generation(setup: Snapshot) -> None:
    _, _, doc, org = setup
    assert org.summary is not None
    org.summary.role_profiles["viewer"] = SummaryProfile(
        focus_topics=["Public accessibility"],
        responsibilities=[],
        glossary={},
    )
    provider = PromptProvider()
    result = service(setup, provider).summarize(
        SummaryRequest(doc_id=doc.id, role="viewer", language="hi"), assigned_obligations=[]
    )
    assert result.not_relevant and not result.points
    assert result.relevance_reason == org.summary.not_relevant_reasons["hi"]
    assert provider.calls == 0


def test_stored_obligation_is_first_and_keeps_its_effective_date(setup: Snapshot) -> None:
    _, _, doc, _ = setup
    provider = PromptProvider()
    result = service(setup, provider).summarize(
        SummaryRequest(doc_id=doc.id, role="finance_officer", language="en"),
        assigned_obligations=[assigned(doc)],
    )
    assert not result.not_relevant
    assert [point.text for point in result.points] == [
        "Reconcile vendor invoices — 2030-05-06",
        REWRITES[FINANCE],
    ]
    assert result.points[0].citations[0].quote == DUTY
    assert result.points[0].citations[0].page == 3
    assert provider.calls == 1


def test_assigned_actions_are_preserved_when_budget_is_exhausted(setup: Snapshot) -> None:
    _, _, doc, org = setup
    assert org.summary is not None
    org.summary.settings.max_length = 1
    provider = PromptProvider()
    result = service(setup, provider).summarize(
        SummaryRequest(doc_id=doc.id, department="finance", language="en"),
        assigned_obligations=[assigned(doc)],
    )
    assert result.points[0].text == "Reconcile vendor invoices — 2030-05-06"
    assert provider.calls == 0


def test_configured_support_check_can_drop_an_exactly_quoted_point(setup: Snapshot) -> None:
    _, _, doc, _ = setup
    provider = PromptProvider()
    reject = FakeProvider(model="support-fixture", response={"verdict": "not supported"})
    verifier = CitationVerifier(VerificationConfig(support_check=True), reject)
    result = service(setup, provider, verifier=verifier).summarize(
        SummaryRequest(doc_id=doc.id, department="finance", language="en"),
        assigned_obligations=[assigned(doc)],
    )
    assert [point.text for point in result.points] == ["Reconcile vendor invoices — 2030-05-06"]
    assert provider.calls == reject.calls == 1


def test_hindi_point_keeps_original_english_citation(setup: Snapshot) -> None:
    _, _, doc, _ = setup
    provider = PromptProvider()
    result = service(setup, provider).summarize(
        SummaryRequest(doc_id=doc.id, department="finance", language="hi"), assigned_obligations=[]
    )
    assert result.language == provider.requests[0]["language"] == "hi"
    assert result.points[0].text == HINDI
    citation = result.points[0].citations[0]
    assert citation.quote == FINANCE
    assert canonical_document(doc).text[citation.char_start : citation.char_end] == FINANCE


def test_invalid_json_shape_is_retried_and_summary_is_cached_by_content(setup: Snapshot) -> None:
    storage, _, doc, _ = setup
    request = SummaryRequest(doc_id=doc.id, department="finance", language="en")
    provider = PromptProvider(invalid_attempts=1)
    first = service(setup, provider, retries=1).summarize(request, assigned_obligations=[])
    assert provider.calls == 2 and first.points

    replacement = PromptProvider()
    assert service(setup, replacement).summarize(request, assigned_obligations=[]) == first
    assert replacement.calls == 0

    storage.source_path(doc.id, doc.filename).write_bytes(b"source-v2")
    assert service(setup, replacement).summarize(request, assigned_obligations=[]) == first
    assert replacement.calls == 1


def test_failed_generation_is_not_cached_and_stored_spans_are_rechecked(setup: Snapshot) -> None:
    _, _, doc, _ = setup
    obligation = assigned(doc)
    assert obligation.citations
    invalid = obligation.citations[0].model_copy(update={"char_start": 0})
    obligation.citations = [invalid]
    provider = PromptProvider(invalid_attempts=1)
    summarizer = service(setup, provider)
    request = SummaryRequest(doc_id=doc.id, department="finance", language="en")
    assert summarizer.summarize(request, assigned_obligations=[obligation]).not_relevant
    good = summarizer.summarize(request, assigned_obligations=[obligation])
    assert [point.text for point in good.points] == [REWRITES[FINANCE]]
    assert provider.calls == 2


def test_assignment_changes_invalidate_the_cached_date(setup: Snapshot) -> None:
    _, _, doc, _ = setup
    provider = PromptProvider()
    summarizer = service(setup, provider)
    request = SummaryRequest(doc_id=doc.id, department="finance", language="en")
    obligation = assigned(doc)
    initial = summarizer.summarize(request, assigned_obligations=[obligation])
    assert initial.points[0].text.endswith("2030-05-06")

    updated = Obligation.model_validate(
        {
            **obligation.model_dump(mode="json"),
            "deadline": {
                "raw_text": "2031-01-15",
                "normalized_date": "2031-01-15",
                "resolved": True,
                "is_relative": False,
                "anchor_date": None,
            },
        }
    )
    refreshed = summarizer.summarize(request, assigned_obligations=[updated])
    assert refreshed.points[0].text.endswith("2031-01-15")
    assert provider.calls == 2


def test_http_summary_accepts_the_core_snapshot_and_checks_document_organization(
    setup: Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, doc, _ = setup
    summarizer = service(setup, PromptProvider())
    monkeypatch.setitem(
        app.dependency_overrides, get_summary_factory, lambda: lambda: nullcontext(summarizer)
    )
    payload = {
        "doc_id": doc.id,
        "org_id": "example",
        "department": "finance",
        "language": "en",
        "assigned_obligations": [assigned(doc).model_dump(mode="json")],
    }
    client = TestClient(app)
    validate_contract(payload, "summary-request.v1.schema.json")
    response = client.post("/v1/summarize", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    validate_contract(result, "summary.v1.schema.json")
    assert result["points"][0]["obligation_id"] == "assigned-1"
    assert result["points"][0]["due_date"] == "2030-05-06"
    assert client.post("/v1/summarize", json={**payload, "org_id": "other"}).status_code == 404
