"""Exercise the Query API with an in-memory collection and deterministic embeddings."""

import socket
from collections.abc import Iterator
from datetime import date
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient, models

from app.retrieval import (
    DenseOnlyRetriever,
    HybridRetriever,
    RetrievalConfig,
    SparseOnlyRetriever,
    create_retriever,
)
from app.vector_store import QdrantStore
from intellidocs_intelligence.chunk_ids import chunk_id
from intellidocs_intelligence.embeddings import Embedding, FakeEmbedder
from intellidocs_intelligence.models import Chunk


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[QdrantStore]:
    def offline(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Retrieval tests must use Qdrant in memory")

    monkeypatch.setattr(socket.socket, "connect", offline)
    client = QdrantClient(":memory:")
    try:
        yield QdrantStore(
            client,
            "retrieval",
            FakeEmbedder(),
            dense_vector_name="semantic",
            sparse_vector_name="lexical",
        )
    finally:
        client.close()


def _chunk(doc_id: str, **overrides: object) -> Chunk:
    text = "needle"
    return Chunk.model_validate(
        {
            "id": chunk_id(doc_id, 0, len(text)),
            "doc_id": doc_id,
            "text": text,
            "page": 1,
            "char_start": 0,
            "char_end": len(text),
            "department": "finance",
            "doc_type": "circular",
            "language": "hi",
            "doc_date": "2026-11-01",
            **overrides,
        }
    )


@pytest.mark.parametrize("rrf_k", [2, 17])
def test_fusion_returns_the_ranked_union_and_breaks_ties(
    store: QdrantStore,
    monkeypatch: pytest.MonkeyPatch,
    rrf_k: int,
) -> None:
    dense_only, overlap, sparse_only = (_chunk(doc_id) for doc_id in ("dense", "both", "sparse"))
    original = store.embedder.embed(["needle"])[0]
    term = original["sparse"]["indices"][0]
    vectors: list[Embedding] = [
        {"dense": original["dense"], "sparse": {"indices": [], "values": []}},
        {"dense": [0.0] * len(original["dense"]), "sparse": {"indices": [term], "values": [1.0]}},
        {
            "dense": [-value for value in original["dense"]],
            "sparse": {"indices": [term], "values": [3.0]},
        },
    ]
    store.upsert_chunks([dense_only, overlap, sparse_only], embeddings=vectors)

    config = RetrievalConfig(candidate_pool_size=2, rrf_k=rrf_k, default_top_k=2)
    query = Mock(wraps=store.client.query_points)
    encoder = Mock(wraps=store.embedder.embed_queries)
    monkeypatch.setattr(store.client, "query_points", query)
    monkeypatch.setattr(store.embedder, "embed_queries", encoder)
    hybrid = HybridRetriever(store, config)
    results = hybrid.retrieve("needle", top_k=5)
    encoder.assert_called_once_with(["needle"])
    if rrf_k == 2:
        query.assert_called_once()
        options = query.call_args.kwargs
        assert options["query"] == models.FusionQuery(fusion=models.Fusion.RRF)
        assert [(part.using, part.limit) for part in options["prefetch"]] == [
            ("semantic", 2),
            ("lexical", 2),
        ]
    else:
        assert [call.kwargs["using"] for call in query.call_args_list] == ["semantic", "lexical"]

    dense = DenseOnlyRetriever(store, config).retrieve("needle")
    sparse = SparseOnlyRetriever(store, config).retrieve("needle")
    assert [item.chunk.doc_id for item in dense] == ["dense", "both"]
    assert [item.chunk.doc_id for item in sparse] == ["sparse", "both"]
    assert {item.source for item in dense} == {"dense"}
    assert {item.source for item in sparse} == {"sparse"}
    assert {item.source for item in results} == {"fused"}
    assert {item.chunk.id for item in results} == {item.chunk.id for item in dense + sparse}
    assert [item.chunk.id for item in results] == [
        overlap.id,
        *sorted([dense_only.id, sparse_only.id]),
    ]
    assert [item.score for item in results] == pytest.approx(
        [
            2 / (rrf_k + 1),
            1 / rrf_k,
            1 / rrf_k,
        ]
    )
    assert hybrid.retrieve("needle", top_k=5) == results
    assert hybrid.retrieve("needle") == results[:2]


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
def test_filters_apply_before_each_candidate_cutoff(store: QdrantStore, mode: str) -> None:
    target = _chunk("target")
    others = [
        _chunk("other-department", department="legal"),
        _chunk("other-type", doc_type="policy"),
        _chunk("other-language", language="en"),
        _chunk("outside", doc_date=date(2026, 11, 2)),
        _chunk("same-metadata"),
        _chunk("undated", doc_date=None),
    ]
    embedding = store.embedder.embed(["needle"])[0]
    stronger_sparse: Embedding = {
        "dense": embedding["dense"],
        "sparse": {"indices": embedding["sparse"]["indices"], "values": [4.0]},
    }
    store.upsert_chunks(
        [target, *others],
        embeddings=[embedding, *([stronger_sparse] * len(others))],
    )
    retriever = create_retriever(store, RetrievalConfig(mode=mode, candidate_pool_size=1))
    where = {
        "department": "finance",
        "document_type": "circular",
        "language": "hi",
        "doc_id": "target",
        "doc_date_from": "2026-11-01",
        "doc_date_to": "2026-11-01",
    }
    assert [hit.chunk for hit in retriever.retrieve("needle", where)] == [target]
    for field, excluded in {
        "department": "operations",
        "document_type": "memo",
        "language": "en",
        "doc_id": "missing",
        "doc_date_to": "2026-11-02",
        "doc_date_from": "2026-10-31",
    }.items():
        changed = {**where, field: excluded}
        if field == "doc_date_to":
            changed.update(doc_date_from="2026-11-02")
        if field == "doc_date_from":
            changed.update(doc_date_to="2026-10-31")
        assert retriever.retrieve("needle", changed) == []


def test_date_bounds_and_undated_chunks(store: QdrantStore) -> None:
    docs = [
        _chunk("lower"),
        _chunk("upper", doc_date="2026-11-30"),
        _chunk("outside", doc_date="2026-12-01"),
        _chunk("undated", doc_date=None),
    ]
    store.upsert_chunks(docs)
    retriever = DenseOnlyRetriever(store, RetrievalConfig())
    for filters, expected in [
        ({"doc_date_from": "2026-11-01", "doc_date_to": "2026-11-30"}, {"lower", "upper"}),
        ({"doc_date_from": "2026-11-30"}, {"upper", "outside"}),
        ({"doc_date_to": "2026-11-01"}, {"lower"}),
        ({"doc_date": "2026-11-30"}, {"upper"}),
    ]:
        assert {hit.chunk.doc_id for hit in retriever.retrieve("needle", filters)} == expected


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
def test_missing_or_empty_index_returns_no_candidates(store: QdrantStore, mode: str) -> None:
    retriever = create_retriever(store, RetrievalConfig(mode=mode))
    assert retriever.retrieve("needle") == []
    store.upsert_chunks([_chunk("removed")])
    store.delete_document("removed")
    assert retriever.retrieve("needle") == []


def test_hybrid_with_empty_sparse_query(
    store: QdrantStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _chunk("dense")
    store.upsert_chunks([item])
    embedding = store.embedder.embed(["needle"])[0]
    monkeypatch.setattr(
        store.embedder,
        "embed_queries",
        lambda _: [
            {**embedding, "sparse": {"indices": [], "values": []}},
        ],
    )
    config = RetrievalConfig()
    assert SparseOnlyRetriever(store, config).retrieve("needle") == []
    [candidate] = HybridRetriever(store, config).retrieve("needle")
    assert candidate.chunk == item and candidate.source == "fused"
    assert candidate.score == pytest.approx(0.5)


def test_factory_selects_mode_and_reads_env(
    store: QdrantStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETRIEVAL_MODE", "sparse")
    monkeypatch.setenv("RETRIEVAL_CANDIDATE_POOL_SIZE", "7")
    monkeypatch.setenv("RETRIEVAL_RRF_K", "22")
    monkeypatch.setenv("RETRIEVAL_DEFAULT_TOP_K", "3")
    retriever = create_retriever(store)
    assert isinstance(retriever, SparseOnlyRetriever)
    assert retriever.config == RetrievalConfig("sparse", 7, 22, 3)
    monkeypatch.setenv("RETRIEVAL_RRF_K", "0")
    with pytest.raises(ValueError, match="RETRIEVAL_RRF_K"):
        create_retriever(store)


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"department": ""}, "nonempty"),
        ({"unknown": "value"}, "Unsupported"),
        ({"doc_type": "memo", "document_type": "policy"}, "disagree"),
        ({"doc_date_from": "2026-02-30"}, "calendar date"),
        ({"doc_date_from": "2026-11-02", "doc_date_to": "2026-11-01"}, "on or before"),
    ],
)
def test_invalid_filters_fail_before_retrieval(
    store: QdrantStore,
    filters: dict[str, str],
    message: str,
) -> None:
    retriever = create_retriever(store, RetrievalConfig())
    with pytest.raises(ValueError, match=message):
        retriever.retrieve("needle", filters)


def test_invalid_query_and_top_k(store: QdrantStore) -> None:
    retriever = create_retriever(store, RetrievalConfig())
    with pytest.raises(ValueError, match="blank"):
        retriever.retrieve(" ")
    for limit in (0, 101, True):
        with pytest.raises(ValueError, match="top_k"):
            retriever.retrieve("needle", top_k=limit)
