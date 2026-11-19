"""Search over indexed chunks with runtime contract and citation checks."""

import socket
from collections.abc import Iterator
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from test_api import validate_contract

from app.chunking import Chunker, ChunkingConfig, canonical_document
from app.reranking import FakeReranker, NoOpReranker
from app.retrieval import DenseOnlyRetriever, HybridRetriever, RetrievalConfig
from app.search import SearchService
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.main import app, get_search_factory
from intellidocs_intelligence.models import Chunk, ParsedDocument


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[QdrantStore]:
    def offline(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Search tests must be offline")

    monkeypatch.setattr(socket.socket, "connect", offline)
    client = QdrantClient(":memory:")
    try:
        yield QdrantStore(client, "search", FakeEmbedder())
    finally:
        app.dependency_overrides.pop(get_search_factory, None)
        client.close()


def _configure(service: SearchService) -> None:
    app.dependency_overrides[get_search_factory] = lambda: lambda: nullcontext(service)


def _respond(**request: object):
    return TestClient(app).post("/v1/search", json={"query": "report", "top_k": 1, **request})


def _block(block_type: str, text: str, order: int, **extra: object) -> dict[str, object]:
    return {"type": block_type, "text": text, "bbox": [0, 0, 1, 1], "reading_order": order, **extra}


def test_search_filters_and_multilingual_canonical_citation(store: QdrantStore) -> None:
    document = ParsedDocument.model_validate(
        {
            "id": "agreement",
            "filename": "test.pdf",
            "mime_type": "application/pdf",
            "source_uri": "file:///test.pdf",
            "status": "ready",
            "pages": [
                {
                    "page": 2,
                    "language": "hi",
                    "blocks": [
                        _block("header_footer", "📄 Header", 0),
                        _block("heading", "Terms", 1),
                        _block("clause", "3.2 विभाग report", 2, clause_number="3.2"),
                    ],
                },
                {
                    "page": 3,
                    "language": "hi",
                    "blocks": [_block("paragraph", "Submit by Friday.", 0)],
                },
            ],
        }
    )
    chunker = Chunker(ChunkingConfig(100, 0, "whitespace"))
    chunks = chunker.chunk(document, department="legal", doc_type="agreement")
    excluded = chunks[-1].model_copy(update={"doc_id": "excluded", "department": "finance"})
    store.upsert_chunks([*chunks, excluded])
    _configure(SearchService(HybridRetriever(store, RetrievalConfig()), FakeReranker()))

    body = {
        "query": "विभाग report",
        "filters": {"department": "legal", "language": "hi", "document_type": "agreement"},
        "top_k": 3,
    }
    response = _respond(**body, debug=True)
    assert response.status_code == 200, response.text
    payload = response.json()
    validate_contract(payload, "search-response.v1.schema.json")
    result = payload["results"][0]
    assert all(row["chunk"]["doc_id"] == document.id for row in payload["results"])
    assert result["rerank_score"] == 2 and result["source"] == "fused"
    [citation] = result["citations"]
    assert citation["doc_id"] == document.id
    assert citation["page"] == citation["page_start"] == 2
    assert citation["page_end"] == 3
    assert citation["clause_number"] == "3.2"
    assert citation["heading_path"] == ["Terms"]
    assert citation["char_start"] > 0
    original = canonical_document(document).text
    assert original[citation["char_start"] : citation["char_end"]] == result["chunk"]["text"]
    assert citation["quote"] == result["chunk"]["text"]
    assert payload["debug"]["retriever"] == "HybridRetriever"
    assert payload["debug"]["reranker"] == "FakeReranker"
    assert set(payload["debug"]["timings_ms"]) == {"retrieve", "rerank", "citations"}
    assert all(value >= 0 for value in payload["debug"]["timings_ms"].values())
    assert "debug" not in _respond(**body).json()
    assert "debug" not in _respond(**body, debug=False).json()


def _legacy_chunk(doc_id: str, text: str) -> Chunk:
    return Chunk(id=doc_id, doc_id=doc_id, page=4, char_start=0, char_end=len(text), text=text)


def test_org_filter_excludes_other_and_untagged_documents(store: QdrantStore) -> None:
    store.upsert_chunks(
        [
            _legacy_chunk("allowed", "report").model_copy(update={"org_id": "example"}),
            _legacy_chunk("other", "report").model_copy(update={"org_id": "another"}),
            _legacy_chunk("legacy", "report"),
        ]
    )
    _configure(SearchService(HybridRetriever(store, RetrievalConfig()), NoOpReranker()))
    response = _respond(top_k=10, filters={"org_id": "example"})
    assert response.status_code == 200
    assert [item["chunk"]["doc_id"] for item in response.json()["results"]] == ["allowed"]


def test_top_k_limits_results_after_reranking_the_full_pool(store: QdrantStore) -> None:
    first, second = _legacy_chunk("other", "Lunch"), _legacy_chunk("match", "report")
    query_vector = store.embedder.embed_queries(["report"])[0]
    opposite = {**query_vector, "dense": [-item for item in query_vector["dense"]]}
    store.upsert_chunks([first, second], embeddings=[query_vector, opposite])
    retriever = DenseOnlyRetriever(
        store, RetrievalConfig(mode="dense", candidate_pool_size=2, default_top_k=1)
    )
    assert retriever.retrieve("report")[0].chunk.doc_id == "other"
    _configure(SearchService(retriever, FakeReranker()))
    response = _respond()
    assert response.status_code == 200, response.text
    validate_contract(response.json(), "search-response.v1.schema.json")
    [result] = response.json()["results"]
    assert result["chunk"]["doc_id"] == "match"
    assert result["score"] < 0 and result["rerank_score"] == 1.0
    [citation] = result["citations"]
    assert citation["page_start"] == citation["page_end"] == 4
    assert citation["heading_path"] == []
    assert "clause_number" not in citation

    _configure(SearchService(retriever, NoOpReranker()))
    [unreranked] = _respond().json()["results"]
    assert unreranked["chunk"]["doc_id"] == "other"
    assert "rerank_score" not in unreranked


def test_empty_index_is_contract_valid(store: QdrantStore) -> None:
    _configure(SearchService(HybridRetriever(store, RetrievalConfig()), NoOpReranker()))
    response = _respond()
    assert response.status_code == 200
    assert response.json() == {"results": []}
    validate_contract(response.json(), "search-response.v1.schema.json")


@pytest.mark.parametrize("query", ["  \t", "", 7])
def test_invalid_query_returns_422_without_calling_retrieval(query: object) -> None:
    def should_not_run():
        pytest.fail("Invalid queries should never invoke search")

    app.dependency_overrides[get_search_factory] = lambda: should_not_run
    try:
        response = _respond(query=query)
    finally:
        app.dependency_overrides.pop(get_search_factory, None)
    assert response.status_code == 422
    [issue] = response.json()["detail"]
    assert issue["loc"] == ["body", "query"] and issue["msg"]
    if query == "  \t":
        assert "non-whitespace" in issue["msg"]


def test_invalid_filters_return_422_with_reason(store: QdrantStore) -> None:
    _configure(SearchService(HybridRetriever(store, RetrievalConfig()), NoOpReranker()))
    response = _respond(filters={"doc_date_from": "2026-02-30"})
    assert response.status_code == 422
    assert "calendar date" in response.json()["detail"]
