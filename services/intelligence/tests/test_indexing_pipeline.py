"""Ingest -> stored parse -> batched fake vectors -> Qdrant memory, offline."""

import hashlib
import json
import socket
from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from qdrant_client import QdrantClient

from app.chunking import Chunker, ChunkingConfig
from app.indexing import IndexingService
from app.storage import LocalStorage
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import Embedding, FakeEmbedder
from intellidocs_intelligence.main import app, get_indexing_factory

FIXTURES = Path(__file__).parent / "fixtures"


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[int] = []

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        self.batches.append(len(texts))
        return super().embed(texts)


@pytest.fixture
def pipeline(ingestion_setup: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[IndexingService]:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Pipeline tests must not call a network")

    monkeypatch.setattr(socket.socket, "connect", fail)
    client = QdrantClient(":memory:")
    embedder = CountingEmbedder()
    store = QdrantStore(client, "test-chunks", embedder)
    service = IndexingService(
        LocalStorage(ingestion_setup),
        Chunker(ChunkingConfig(8, 2, "whitespace")),
        embedder,
        store,
        batch_size=1,
    )
    app.dependency_overrides[get_indexing_factory] = lambda: lambda: nullcontext(service)
    try:
        yield service
    finally:
        app.dependency_overrides.pop(get_indexing_factory, None)
        client.close()


def upload(**fields: str) -> dict:
    response = TestClient(app).post(
        "/v1/ingest",
        files={
            "file": ("upload.docx", (FIXTURES / "upload.docx").read_bytes()),
        },
        data=fields,
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_ingest_index_metadata_batches_and_cache(pipeline: IndexingService) -> None:
    parsed = upload(department="legal", doc_type="agreement", doc_date="2026-11-15")
    identifier = parsed["document_id"]
    response = TestClient(app).post("/v1/index", json={"doc_id": identifier})
    assert response.status_code == 200, response.text
    schema = Path(__file__).resolve().parents[3] / "contracts/schemas/index-result.v1.schema.json"
    Draft202012Validator(json.loads(schema.read_text())).validate(response.json())
    assert response.json()["chunk_count"] >= 3
    embedder = pipeline.embedder
    assert isinstance(embedder, CountingEmbedder)
    assert embedder.batches == [1] * response.json()["chunk_count"]
    store = pipeline.store
    assert isinstance(store, QdrantStore)
    points, _offset = store.client.scroll(store.collection_name, with_vectors=True)
    assert len(points) == response.json()["chunk_count"]
    for point in points:
        assert point.payload
        assert point.payload["doc_id"] == identifier
        assert point.payload["department"] == "legal"
        assert point.payload["doc_type"] == "agreement"
        assert point.payload["doc_date"] == "2026-11-15"
        assert point.payload["block_ids"]
        assert isinstance(point.vector, dict) and {"dense", "sparse"} == set(point.vector)
    second = TestClient(app).post("/v1/index", json={"doc_id": identifier})
    assert second.status_code == 200
    assert second.json()["warnings"] == ["Reused unchanged-content embeddings."]
    assert len(embedder.batches) == len(points)
    assert store.client.count(store.collection_name, exact=True).count == len(points)
    assert pipeline.storage.load_index_cache(identifier)["status"] == "indexed"
    fresh_embedder = CountingEmbedder()
    fresh_service = IndexingService(
        LocalStorage(pipeline.storage.root), pipeline.chunker, fresh_embedder, store, batch_size=2
    )
    assert fresh_service.index(identifier).chunk_count == len(points)
    assert fresh_embedder.batches == []


def test_metadata_refresh_reuses_embeddings(pipeline: IndexingService) -> None:
    identifier = upload()["document_id"]
    pipeline.index(identifier)
    embedder = pipeline.embedder
    assert isinstance(embedder, CountingEmbedder)
    batches = list(embedder.batches)
    parsed = pipeline.storage.load_result(identifier)
    assert parsed.document.department is None and parsed.document.doc_type is None
    parsed.document.department = "finance"
    pipeline.storage.save_result(identifier, parsed)
    pipeline.index(identifier)
    assert embedder.batches == batches
    store = pipeline.store
    assert isinstance(store, QdrantStore)
    assert all(
        point.payload and point.payload["department"] == "finance"
        for point in store.client.scroll(store.collection_name)[0]
    )


def test_content_change_re_embeds_and_prunes_obsolete_spans(pipeline: IndexingService) -> None:
    identifier = upload()["document_id"]
    pipeline.index(identifier)
    embedder = pipeline.embedder
    assert isinstance(embedder, CountingEmbedder)
    before = len(embedder.batches)
    parsed = pipeline.storage.load_result(identifier)
    path = pipeline.storage.source_path(identifier, parsed.document.filename)
    path.write_bytes(b"new original fixture source")
    parsed.document.content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    parsed.document.pages[0].blocks = [parsed.document.pages[0].blocks[0]]
    pipeline.storage.save_result(identifier, parsed)
    indexed = pipeline.index(identifier)
    assert len(embedder.batches) > before
    store = pipeline.store
    assert isinstance(store, QdrantStore)
    assert store.client.count(store.collection_name).count == indexed.chunk_count == 1


def test_retry_after_upsert_failure_reuses_computed_vectors(
    pipeline: IndexingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = upload()["document_id"]
    store = pipeline.store
    original = store.upsert_chunks

    def fail(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("Qdrant unavailable")

    monkeypatch.setattr(store, "upsert_chunks", fail)
    response = TestClient(app).post("/v1/index", json={"doc_id": identifier})
    assert response.status_code == 503
    assert pipeline.storage.load_index_cache(identifier)["status"] == "prepared"
    assert isinstance(pipeline.embedder, CountingEmbedder)
    before = list(pipeline.embedder.batches)
    monkeypatch.setattr(store, "upsert_chunks", original)
    assert TestClient(app).post("/v1/index", json={"doc_id": identifier}).status_code == 200
    assert pipeline.embedder.batches == before


def test_optional_ingest_indexing_and_failure_retains_parse(
    pipeline: IndexingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = upload(index="true", department="legal")
    assert body["index_results"][0]["doc_id"] == body["document_id"]

    def fail(_doc: str):
        raise RuntimeError("Backend unavailable")

    monkeypatch.setattr(pipeline, "index", fail)
    response = TestClient(app).post(
        "/v1/ingest",
        data={"index": "true"},
        files={
            "file": ("upload.docx", (FIXTURES / "upload.docx").read_bytes()),
        },
    )
    assert response.status_code == 503
    assert response.json()["index_error"]
    assert pipeline.storage.load_result(response.json()["document_id"]).document.status == "ready"


def test_config_defaults_and_request_precedence(
    pipeline: IndexingService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / "config/orgs/example.yaml").read_text())
    config["upload_defaults"] = {
        "department": "legal",
        "doc_type": "agreement",
        "doc_date": "2026-01-01",
    }
    (tmp_path / "example.yaml").write_text(json.dumps(config))
    monkeypatch.setenv("ORG_CONFIG_DIR", str(tmp_path))
    body = upload(department="finance")
    assert body["parse_result"]["document"]["department"] == "finance"
    assert body["parse_result"]["document"]["doc_type"] == "agreement"
    assert body["parse_result"]["document"]["doc_date"] == "2026-01-01"


def test_optional_indexing_includes_email_attachments(pipeline: IndexingService) -> None:
    response = TestClient(app).post(
        "/v1/ingest",
        data={"index": "true", "department": "legal", "doc_type": "agreement"},
        files={"file": ("upload.eml", (FIXTURES / "upload.eml").read_bytes())},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    child = body["parse_result"]["attachments"][0]["document"]["id"]
    identifiers = {body["document_id"], child}
    assert {result["doc_id"] for result in body["index_results"]} == identifiers
    store = pipeline.store
    assert isinstance(store, QdrantStore)
    points, _ = store.client.scroll(store.collection_name, limit=100)
    assert {point.payload["doc_id"] for point in points if point.payload} == identifiers
    assert all(point.payload and point.payload["department"] == "legal" for point in points)


def test_incomplete_upsert_is_not_marked_indexed(
    pipeline: IndexingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = upload()["document_id"]
    monkeypatch.setattr(pipeline.store, "upsert_chunks", lambda *_args, **_kwargs: 0)
    response = TestClient(app).post("/v1/index", json={"doc_id": identifier})
    assert response.status_code == 503
    assert pipeline.storage.load_index_cache(identifier)["status"] == "prepared"


def test_index_missing_invalid_and_changed_source(pipeline: IndexingService) -> None:
    client = TestClient(app)
    assert client.post("/v1/index", json={"doc_id": str(uuid4())}).status_code == 404
    assert client.post("/v1/index", json={"doc_id": "../../bad"}).status_code == 400
    identifier = upload()["document_id"]
    parsed = pipeline.storage.load_result(identifier)
    pipeline.storage.source_path(identifier, parsed.document.filename).write_bytes(b"changed")
    assert client.post("/v1/index", json={"doc_id": identifier}).status_code == 400


def test_model_identity_invalidates_embedding_cache(pipeline: IndexingService) -> None:
    identifier = upload()["document_id"]
    pipeline.index(identifier)
    assert isinstance(pipeline.embedder, CountingEmbedder)
    before = len(pipeline.embedder.batches)
    pipeline.embedder.model_name = "fake-version-2"
    assert pipeline.index(identifier).embedding_model == "fake-version-2"
    assert len(pipeline.embedder.batches) > before


def test_explicit_metadata_validation(pipeline: IndexingService) -> None:
    for fields in ({"doc_date": "0"}, {"doc_date": "2026-02-30"}, {"department": "unknown"}):
        response = TestClient(app).post(
            "/v1/ingest",
            files={
                "file": ("upload.docx", (FIXTURES / "upload.docx").read_bytes()),
            },
            data=fields,
        )
        assert response.status_code in {400, 422}
