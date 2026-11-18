"""Offline answer pipeline integration, validation, and safe failure behavior."""

import json
import socket
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from test_api import validate_contract

from app.answering import AnswerService
from app.chunking import Chunker, ChunkingConfig, canonical_document
from app.generation import ClaimGenerator, GenerationConfig
from app.reranking import FakeReranker
from app.retrieval import HybridRetriever, RetrievalConfig
from app.search import SearchService
from app.storage import LocalStorage
from app.vector_store import QdrantStore
from app.verification import CitationVerifier, VerificationConfig
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.main import app, get_answer_factory
from intellidocs_intelligence.models import ParsedDocument, ParseResult


@pytest.fixture
def resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    def offline(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Answer tests must be offline")

    monkeypatch.setattr(socket.socket, "connect", offline)
    client = QdrantClient(":memory:")
    storage = LocalStorage(tmp_path)
    try:
        yield QdrantStore(client, "answers", FakeEmbedder()), storage
    finally:
        app.dependency_overrides.pop(get_answer_factory, None)
        client.close()


def setup_answer(resources: tuple, response: dict | None = None) -> tuple:
    store, storage = resources
    document = ParsedDocument.model_validate(
        {
            "id": str(uuid4()),
            "filename": "source.pdf",
            "mime_type": "application/pdf",
            "source_uri": "file:///source.pdf",
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
                for page, text in [
                    (1, "Reports must be submitted."),
                    (2, "The deadline is Friday."),
                ]
            ],
        }
    )
    storage.save(document.id, document.filename, b"test-source")
    storage.save_result(
        document.id,
        ParseResult(document=document, parser_name="fixture", parser_version="1", warnings=[]),
    )
    chunks = Chunker(ChunkingConfig(100, 0, "whitespace")).chunk(document, department="records")
    store.upsert_chunks(chunks)
    excluded = chunks[0].model_copy(update={"doc_id": str(uuid4()), "department": "other"})
    store.upsert_chunks([excluded])
    provider = FakeProvider(
        model="test-model",
        response=response
        or {
            "claims": [
                {
                    "text": "The deadline is Friday.",
                    "chunk_id": chunks[0].id,
                    "quote": "The deadline is Friday.",
                },
                {
                    "text": "Unsupported fiction.",
                    "chunk_id": "wrong",
                    "quote": "Invented evidence.",
                },
            ]
        },
    )
    service = AnswerService(
        SearchService(HybridRetriever(store, RetrievalConfig()), FakeReranker()),
        ClaimGenerator(provider, GenerationConfig(0)),
        CitationVerifier(VerificationConfig()),
        storage,
    )
    app.dependency_overrides[get_answer_factory] = lambda: lambda: nullcontext(service)
    return document, provider, service


def respond(**fields: object):
    return TestClient(app).post(
        "/v1/answer",
        json={
            "query": "deadline",
            "language": "en",
            "top_k": 1,
            "filters": {"department": "records"},
            **fields,
        },
    )


def test_answer_pipeline_contract_filters_and_exact_page_offsets(resources: tuple) -> None:
    document, provider, service = setup_answer(resources)
    response = respond()
    assert response.status_code == 200, response.text
    answer = response.json()
    validate_contract(answer, "answer.v1.schema.json")
    assert answer["answer_text"] == "The deadline is Friday."
    assert len(answer["claims"]) == 1 and answer["claims"][0]["status"] == "verified"
    citation = answer["claims"][0]["citations"][0]
    assert citation["page"] == citation["page_start"] == citation["page_end"] == 2
    assert (
        canonical_document(document).text[citation["char_start"] : citation["char_end"]]
        == citation["quote"]
    )
    assert len(answer["retrieved_chunk_ids"]) == 1
    assert answer["model"] == provider.model and answer["verifier_version"]
    assert not answer["abstained"]
    # Stage order and requested language are observable in the generation prompt.
    captured = []
    original = service.generator.provider.generate

    def record(prompt, schema):
        captured.append(json.loads(prompt.split("REQUEST_JSON\n")[1]))
        return original(prompt, schema)

    service.generator.provider.generate = record
    assert respond(language="hi").status_code == 200
    assert captured[0]["language"] == "hi"
    assert len(captured[0]["chunks"]) == 1


@pytest.mark.parametrize("case", ["invalid", "empty", "unverified", "missing_source"])
def test_abstention_when_generation_or_sources_do_not_support_an_answer(
    resources: tuple, case: str
) -> None:
    document, provider, _ = setup_answer(resources)
    if case == "invalid":
        provider.response = {"wrong": "shape"}
    elif case == "empty":
        provider.response = {"claims": []}
    elif case == "unverified":
        provider.response["claims"] = provider.response["claims"][1:]
    else:
        resources[1].delete(document.id)
    response = respond()
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["abstained"] and answer["abstain_reason"]
    assert answer["claims"] == [] and answer["answer_text"] == ""
    validate_contract(answer, "answer.v1.schema.json")


def test_empty_index_skips_llm_and_invalid_filters_are_422(resources: tuple) -> None:
    _, provider, _ = setup_answer(resources)
    answer = respond(filters={"doc_id": "not-indexed"}).json()
    assert answer["abstained"] and provider.calls == 0
    response = respond(filters={"doc_date_from": "2026-02-30"})
    assert response.status_code == 422 and "calendar date" in response.json()["detail"]


@pytest.mark.parametrize(
    "fields", [{"query": "  "}, {"top_k": 0}, {"language": "e"}, {"verify": False}]
)
def test_invalid_requests_and_http_verifier_bypass_are_rejected(
    resources: tuple, fields: dict
) -> None:
    _, provider, _ = setup_answer(resources)
    assert respond(**fields).status_code == 422
    assert provider.calls == 0


def test_provider_transport_failure_returns_503_without_exposing_error_details(
    resources: tuple,
) -> None:
    _, _, service = setup_answer(resources)

    def fail(*_args: object, **_kwargs: object):
        raise TimeoutError("sensitive provider failure details")

    service.generator.provider.generate = fail
    response = respond()
    assert response.status_code == 503
    assert response.json() == {"detail": "Answer service unavailable"}
