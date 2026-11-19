"""Stored input through indexing, candidate filtering, and a verified HTTP result."""

import json
import socket
from collections.abc import Iterator
from contextlib import nullcontext
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from test_api import validate_contract

from app.chunking import Chunker, ChunkingConfig, canonical_document
from app.extraction import CandidateDetector, ObligationExtractor
from app.extraction.service import ExtractionService
from app.indexing import IndexingService
from app.storage import LocalStorage
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.main import app, get_extraction_factory, get_indexing_factory

QUOTE = "Finance Officer must file the report by 5 December 2026."
UNRESOLVED_QUOTE = "External Supplier must file the report by 5 December 2026."


class PolicyProvider(FakeProvider):
    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        chunks = json.loads(prompt.split("CHUNKS_JSON\n", 1)[1])
        sources = [(QUOTE, "Finance Officer"), (UNRESOLVED_QUOTE, "External Supplier")]
        self.response = {
            "obligations": [
                {
                    "chunk_id": chunk["chunk_id"],
                    "action": "File the report",
                    "owner_text": owner,
                    "deadline_text": "by 5 December 2026",
                    "condition": None,
                    "obligation_type": "reporting",
                    "quote": quote,
                }
                for chunk in chunks
                for quote, owner in sources
                if quote in chunk["text"]
            ],
        }
        assert all(any(quote in chunk["text"] for quote, _ in sources) for chunk in chunks)
        return super().generate(prompt, schema)


@pytest.fixture
def pipeline(
    ingestion_setup: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[LocalStorage, QdrantStore, PolicyProvider]]:
    def offline(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Extraction API tests must be offline")

    monkeypatch.setattr(socket.socket, "connect", offline)
    client = QdrantClient(":memory:")
    embedder = FakeEmbedder()
    store = QdrantStore(client, "extraction", embedder)
    storage = LocalStorage(ingestion_setup)
    indexer = IndexingService(
        storage,
        Chunker(ChunkingConfig(100, 0, "whitespace")),
        embedder,
        store,
        2,
    )
    provider = PolicyProvider("test")
    extractor = ExtractionService(
        storage,
        store,
        lambda org: ObligationExtractor(CandidateDetector(org), provider),
    )
    app.dependency_overrides[get_indexing_factory] = lambda: lambda: nullcontext(indexer)
    app.dependency_overrides[get_extraction_factory] = lambda: lambda: nullcontext(extractor)
    try:
        yield storage, store, provider
    finally:
        app.dependency_overrides.pop(get_indexing_factory)
        app.dependency_overrides.pop(get_extraction_factory)
        client.close()


def ingest(client: TestClient, text: str) -> str:
    email = EmailMessage()
    email["Subject"] = "Policy"
    email.set_content(text)
    result = client.post(
        "/v1/ingest",
        files={"file": ("policy.eml", email.as_bytes(), "message/rfc822")},
        data={"org_id": "example"},
    )
    assert result.status_code == 202, result.text
    assert result.json()["parse_result"]["org_id"] == "example"
    return result.json()["document_id"]


def test_extract_uses_stored_index_and_org_and_returns_stable_citations(
    pipeline: tuple[LocalStorage, QdrantStore, PolicyProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, _, provider = pipeline
    with TestClient(app) as client:
        identifier = ingest(client, QUOTE)
        decoy = ingest(client, "Unrelated owner must send something else.")
        assert client.post("/v1/index", json={"doc_id": decoy}).status_code == 200
        assert client.post("/v1/index", json={"doc_id": identifier}).status_code == 200
        monkeypatch.setenv("DEFAULT_ORG_ID", "different-from-ingestion")
        response = client.post("/v1/extract-obligations", json={"doc_id": identifier})
        again = client.post("/v1/extract-obligations", json={"doc_id": identifier})
    assert response.status_code == again.status_code == 200, response.text
    result = response.json()
    assert result == again.json() and provider.calls == 2
    validate_contract(result, "extraction-result.v1.schema.json")
    [item] = result["obligations"]
    assert item["status"] == "verified" and item["owner"]["resolved_department"] == "finance"
    assert item["confidence"] > 0.5
    assert item["confidence_breakdown"]["quote_exact"] > 0
    assert sum(item["confidence_breakdown"].values()) == pytest.approx(item["confidence"])
    assert item["deadline"]["normalized_date"] == "2026-12-05"
    [citation] = item["citations"]
    text = canonical_document(storage.load_result(identifier).document).text
    assert text[citation["char_start"] : citation["char_end"]] == QUOTE


def test_extract_returns_scores_on_both_sides_of_routing_threshold(
    pipeline: tuple[LocalStorage, QdrantStore, PolicyProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXTRACTION_CONFIDENCE_WEIGHTS", raising=False)
    with TestClient(app) as client:
        identifier = ingest(client, f"{QUOTE}\n\n{UNRESOLVED_QUOTE}")
        assert client.post("/v1/index", json={"doc_id": identifier}).status_code == 200
        response = client.post("/v1/extract-obligations", json={"doc_id": identifier})
    assert response.status_code == 200, response.text
    verified, review = sorted(response.json()["obligations"], key=lambda item: -item["confidence"])
    assert verified["status"] == "verified" and verified["confidence"] == pytest.approx(0.95)
    assert review["status"] == "needs_review" and review["confidence"] == pytest.approx(0.4)
    assert review["confidence_breakdown"]["owner_resolved"] == 0
    assert review["citations"][0]["quote"] == UNRESOLVED_QUOTE


def test_extract_requires_index_and_rejects_missing_chunks(
    pipeline: tuple[LocalStorage, QdrantStore, PolicyProvider],
) -> None:
    _, store, provider = pipeline
    with TestClient(app) as client:
        assert (
            client.post("/v1/extract-obligations", json={"doc_id": str(uuid4())}).status_code == 404
        )
        identifier = ingest(client, QUOTE)
        assert (
            client.post("/v1/extract-obligations", json={"doc_id": identifier}).status_code == 409
        )
        assert client.post("/v1/index", json={"doc_id": identifier}).status_code == 200
        store.delete_document(identifier)
        assert (
            client.post("/v1/extract-obligations", json={"doc_id": identifier}).status_code == 409
        )
        no_candidates = ingest(client, "This statement is informational.")
        assert client.post("/v1/index", json={"doc_id": no_candidates}).status_code == 200
        empty = client.post("/v1/extract-obligations", json={"doc_id": no_candidates})
    assert empty.status_code == 200 and empty.json()["stats"]["total"] == 0
    assert provider.calls == 0
