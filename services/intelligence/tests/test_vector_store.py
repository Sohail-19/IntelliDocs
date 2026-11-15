"""Vector persistence tests: Qdrant local memory only; no server or ranking."""

import socket
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient, models

from app.vector_store import QdrantStore
from app.vector_store.qdrant_store import PAYLOAD_INDEXES
from intellidocs_intelligence.chunk_ids import chunk_id
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.models import Chunk


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Vector-store tests must use Qdrant in memory")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "create_connection", fail)


@pytest.fixture
def client() -> Iterator[QdrantClient]:
    local = QdrantClient(":memory:")
    try:
        yield local
    finally:
        local.close()


def chunk(
    doc_id: str = "doc-1",
    start: int = 0,
    department: str = "legal",
    day: date | None = date(2026, 11, 15),
) -> Chunk:
    identifier = chunk_id(doc_id, start, start + 16)
    return Chunk(
        id=identifier,
        chunk_id=identifier,
        doc_id=doc_id,
        text="Original report.",
        page=1,
        page_start=1,
        page_end=2,
        char_start=start,
        char_end=start + 16,
        block_ids=["block-1", "block-2"],
        clause_number="3.2",
        heading_path=["Section 3", "3.2"],
        language="en",
        department=department,
        doc_type="agreement",
        doc_date=day,
    )


def test_named_vectors_dimension_and_payload(client: QdrantClient) -> None:
    store = QdrantStore(client, "chunks", FakeEmbedder(dimensions=7))
    source = chunk()
    assert store.upsert_chunks([source]) == 1
    points, _offset = client.scroll("chunks", with_vectors=True)
    assert len(points) == 1
    point = points[0]
    assert point.id == source.chunk_id
    assert point.payload == source.model_dump(mode="json", exclude_none=True)
    assert Chunk.model_validate(point.payload) == source
    assert isinstance(point.vector, dict)
    assert len(point.vector["dense"]) == 7
    assert isinstance(point.vector["sparse"], models.SparseVector)
    info = client.get_collection("chunks")
    assert isinstance(info.config.params.vectors, dict)
    assert info.config.params.vectors["dense"].size == 7
    assert info.config.params.vectors["dense"].distance == models.Distance.COSINE
    assert set(info.config.params.sparse_vectors or {}) == {"sparse"}


def test_all_payload_indexes_requested_once(
    client: QdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_index = Mock(wraps=client.create_payload_index)
    monkeypatch.setattr(client, "create_payload_index", create_index)
    store = QdrantStore(client, "chunks", FakeEmbedder())
    store.upsert_chunks([chunk()])
    store.upsert_chunks([chunk()])
    assert create_index.call_count == 5
    actual = {
        call.kwargs["field_name"]: call.kwargs["field_schema"]
        for call in create_index.call_args_list
    }
    assert actual == PAYLOAD_INDEXES
    assert actual["doc_date"] == models.PayloadSchemaType.DATETIME
    assert all(call.kwargs["wait"] for call in create_index.call_args_list)


def test_re_upsert_replaces_and_keeps_point_count(client: QdrantClient) -> None:
    store = QdrantStore(client, "chunks", FakeEmbedder())
    chunks = [chunk(start=0), chunk(start=20)]
    store.upsert_chunks(chunks)
    original_ids = {point.id for point in client.scroll("chunks")[0]}
    changed = [
        item.model_copy(update={"text": "Modified report.", "department": "finance"})
        for item in chunks
    ]
    store.upsert_chunks(changed)
    assert client.count("chunks", exact=True).count == 2
    points, _offset = client.scroll("chunks")
    assert {point.id for point in points} == original_ids
    assert all(point.payload and point.payload["department"] == "finance" for point in points)
    assert all(point.payload and point.payload["text"] == "Modified report." for point in points)


def test_backend_identity_ignores_legacy_caller_aliases(client: QdrantClient) -> None:
    store = QdrantStore(client, "chunks", FakeEmbedder())
    first = chunk().model_copy(update={"id": "legacy-1", "chunk_id": "old-hash"})
    second = first.model_copy(update={"id": "legacy-2", "chunk_id": "changed-hash"})
    store.upsert_chunks([first])
    store.upsert_chunks([second])
    points, _offset = client.scroll("chunks")
    assert len(points) == 1
    assert points[0].id == chunk_id(first.doc_id, first.char_start, first.char_end)
    assert points[0].payload == second.model_dump(mode="json", exclude_none=True)


def test_delete_document_removes_only_its_chunks(client: QdrantClient) -> None:
    store = QdrantStore(client, "chunks", FakeEmbedder())
    store.delete_document("missing")  # No collection or model initialization required.
    store.upsert_chunks([chunk(start=0), chunk(start=20), chunk(doc_id="other")])
    store.delete_document("doc-1")
    points, _offset = client.scroll("chunks")
    assert len(points) == 1
    assert points[0].payload and points[0].payload["doc_id"] == "other"
    store.delete_document("doc-1")
    assert client.count("chunks", exact=True).count == 1


def test_department_and_inclusive_date_range_filter(client: QdrantClient) -> None:
    store = QdrantStore(client, "chunks", FakeEmbedder())
    store.upsert_chunks(
        [
            chunk("before", day=date(2026, 10, 31)),
            chunk("lower", day=date(2026, 11, 1)),
            chunk("middle", day=date(2026, 11, 15)),
            chunk("upper", day=date(2026, 11, 30)),
            chunk("after", day=date(2026, 12, 1)),
            chunk("other-department", department="finance", day=date(2026, 11, 15)),
            chunk("undated", day=None),
        ]
    )
    points, _offset = client.scroll(
        "chunks",
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(key="department", match=models.MatchValue(value="legal")),
                models.FieldCondition(
                    key="doc_date",
                    range=models.DatetimeRange(
                        gte=datetime(2026, 11, 1, tzinfo=UTC),
                        lte=datetime(2026, 11, 30, tzinfo=UTC),
                    ),
                ),
            ]
        ),
    )
    assert {point.payload["doc_id"] for point in points if point.payload} == {
        "lower",
        "middle",
        "upper",
    }


def test_existing_collection_wrong_dimension_is_not_modified(client: QdrantClient) -> None:
    QdrantStore(client, "chunks", FakeEmbedder(dimensions=5)).upsert_chunks([chunk()])
    with pytest.raises(ValueError, match="incompatible"):
        QdrantStore(client, "chunks", FakeEmbedder(dimensions=8)).upsert_chunks([chunk("new")])
    assert client.count("chunks", exact=True).count == 1


def test_missing_named_sparse_vector_is_rejected(client: QdrantClient) -> None:
    client.create_collection(
        "chunks",
        vectors_config={
            "dense": models.VectorParams(
                size=16,
                distance=models.Distance.COSINE,
            )
        },
    )
    with pytest.raises(ValueError, match="incompatible"):
        QdrantStore(client, "chunks", FakeEmbedder()).upsert_chunks([chunk()])


def test_bad_embedding_rejected_before_collection_mutation(
    client: QdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = FakeEmbedder()

    def bad(_texts: Any) -> Any:
        return [{"dense": [float("nan")], "sparse": {"indices": [], "values": []}}]

    monkeypatch.setattr(embedder, "embed", bad)
    with pytest.raises(ValueError, match="finite"):
        QdrantStore(client, "chunks", embedder).upsert_chunks([chunk()])
    assert not client.collection_exists("chunks")


def test_empty_and_duplicate_batches(client: QdrantClient) -> None:
    store = QdrantStore(client, "chunks", FakeEmbedder())
    assert store.upsert_chunks([]) == 0
    assert not client.collection_exists("chunks")
    with pytest.raises(ValueError, match="Duplicate"):
        store.upsert_chunks([chunk(), chunk()])
    with pytest.raises(NotImplementedError, match="not implemented"):
        store.search({"dense": [], "sparse": {"indices": [], "values": []}}, limit=1)
