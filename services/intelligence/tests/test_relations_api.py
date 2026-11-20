"""Verify the relation endpoint, persisted parse evidence, and the indexed shortlist."""

import socket
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from test_api import validate_contract
from test_relation_classifier import OLD_ORDER, REPLACEMENT

from app.chunking import Chunker, ChunkingConfig, canonical_document
from app.indexing import IndexingService
from app.relations.classifier import RelationClassifier
from app.relations.service import RelationService
from app.storage import LocalStorage
from app.vector_store import QdrantStore
from intellidocs_intelligence.cache import DiskCache
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.main import app, get_relation_factory
from intellidocs_intelligence.models import Page, ParsedDocument, ParseResult, TextBlock


@pytest.fixture
def pipeline(
    ingestion_setup: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[LocalStorage, IndexingService, FakeProvider]]:
    def offline(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Relation tests must be offline")

    monkeypatch.setattr(socket.socket, "connect", offline)
    storage = LocalStorage(ingestion_setup)
    qdrant = QdrantClient(":memory:")
    embedder = FakeEmbedder()
    store = QdrantStore(qdrant, "relations", embedder)
    indexer = IndexingService(
        storage, Chunker(ChunkingConfig(100, 0, "whitespace")), embedder, store, 4
    )
    provider = FakeProvider(
        "fixture",
        {
            "relation_type": "supersedes",
            "scope": "whole_document",
            "source_quote": REPLACEMENT,
            "target_quote": OLD_ORDER,
        },
    )
    cache = DiskCache(ingestion_setup / "relation-cache")
    service = RelationService(
        storage, store, lambda org: RelationClassifier(provider, org, cache=cache)
    )
    app.dependency_overrides[get_relation_factory] = lambda: lambda: nullcontext(service)
    try:
        yield storage, indexer, provider
    finally:
        app.dependency_overrides.pop(get_relation_factory)
        qdrant.close()


def save(storage: LocalStorage, name: str, text: str, day: date, org: str = "example") -> str:
    doc_id = str(uuid4())
    storage.save(doc_id, name, b"%PDF fixture")
    document = ParsedDocument(
        id=doc_id,
        filename=name,
        mime_type="application/pdf",
        source_uri=storage.source_uri(doc_id, name),
        doc_type="agreement",
        doc_date=day,
        status="ready",
        pages=[
            Page(
                page=1,
                language="en",
                blocks=[
                    TextBlock(type="paragraph", text=text, reading_order=0, bbox=[1, 2, 10, 20])
                ],
            )
        ],
    )
    storage.save_result(
        doc_id,
        ParseResult(
            document=document, org_id=org, parser_name="test", parser_version="v1", warnings=[]
        ),
    )
    return doc_id


def test_endpoint_indexes_finds_and_verifies_both_spans(
    pipeline: tuple[LocalStorage, IndexingService, FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, indexer, provider = pipeline
    source = save(storage, "new.pdf", REPLACEMENT, date(2026, 1, 1))
    target = save(storage, "Order 12.pdf", OLD_ORDER, date(2025, 1, 1))
    future = save(storage, "Order 12.pdf", OLD_ORDER, date(2027, 1, 1))
    other = save(storage, "Order 12.pdf", OLD_ORDER, date(2025, 1, 1), org="example2")
    for doc_id in (source, target, future, other):
        indexer.index(doc_id)
    monkeypatch.setenv("DEFAULT_ORG_ID", "example2")
    with TestClient(app) as client:
        first = client.post("/v1/detect-relations", json={"doc_id": source})
        second = client.post("/v1/detect-relations", json={"doc_id": source})
    assert first.status_code == second.status_code == 200, first.text
    result = first.json()
    validate_contract(result, "relation-result.v1.schema.json")
    assert result["doc_id"] == source and result["warnings"] == []
    (relation,) = result["relations"]
    assert relation["status"] == "proposed" and relation["needs_review"] is False
    assert relation["confidence"] == 1 and relation["target_doc_id"] == target
    assert relation["id"] == second.json()["relations"][0]["id"] and provider.calls == 1
    for side, expected_doc in (("source_citations", source), ("target_citations", target)):
        (citation,) = relation[side]
        text = canonical_document(storage.load_result(expected_doc).document).text
        assert citation["doc_id"] == expected_doc
        assert text[citation["char_start"] : citation["char_end"]] == citation["quote"]
        assert citation["boxes"][0]["page"] == citation["page"] == 1


def test_endpoint_requires_complete_source_index(
    pipeline: tuple[LocalStorage, IndexingService, FakeProvider],
) -> None:
    storage, indexer, provider = pipeline
    document = save(storage, "new.pdf", REPLACEMENT, date(2026, 1, 1))
    with TestClient(app) as client:
        assert client.post("/v1/detect-relations", json={"doc_id": ""}).status_code == 422
        missing = client.post("/v1/detect-relations", json={"doc_id": str(uuid4())})
        assert missing.status_code == 404
        unindexed = client.post("/v1/detect-relations", json={"doc_id": document})
        assert unindexed.status_code == 409 and provider.calls == 0
        indexer.index(document)
        empty = client.post("/v1/detect-relations", json={"doc_id": document})
        assert empty.status_code == 200 and empty.json()["relations"] == []
        indexer.store.delete_document(document, org_id="example")
        assert client.post("/v1/detect-relations", json={"doc_id": document}).status_code == 409
    assert provider.calls == 0
