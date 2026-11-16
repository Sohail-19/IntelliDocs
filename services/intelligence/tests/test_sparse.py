"""BM25 math and real dense/sparse Qdrant queries with offline model boundaries."""

import math
import socket
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient, models

from app.indexing import _cached_embedder
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import (
    BgeM3Embedder,
    Embedding,
    FakeEmbedder,
    SentenceTransformerEmbedder,
    SparseOverrideEmbedder,
    embedder_from_env,
    embedding_settings_key,
)
from intellidocs_intelligence.models import Chunk
from intellidocs_intelligence.sparse import Bm25Encoder, term_id, tokenize


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Sparse tests must not use a network")

    monkeypatch.setattr(socket.socket, "connect", fail)
    for name in ("SPARSE_EMBEDDING_BACKEND", "BM25_K1", "BM25_B", "BM25_AVG_DOC_LENGTH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBEDDING_BACKEND", "sentence_transformers")
    monkeypatch.setenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
    monkeypatch.setenv("EMBEDDING_DEVICE", "cpu")
    monkeypatch.setenv("EMBEDDING_DOCUMENT_PROMPT", "passage: ")
    monkeypatch.setenv("EMBEDDING_QUERY_PROMPT", "query: ")


@pytest.fixture
def stub_model(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str]]:
    calls: list[tuple[list[str], str]] = []

    def encode(
        _self: SentenceTransformerEmbedder, texts: list[str], prompt: str
    ) -> list[Embedding]:
        calls.append((list(texts), prompt))
        # Equal dense scores make sparse matching observable in the fusion test.
        return [{"dense": [1.0, 0.0], "sparse": {"indices": [], "values": []}} for _ in texts]

    monkeypatch.setattr(SentenceTransformerEmbedder, "_encode", encode)
    return calls


@pytest.fixture
def client() -> Iterator[QdrantClient]:
    local = QdrantClient(":memory:")
    try:
        yield local
    finally:
        local.close()


def chunk(identifier: str, text: str) -> Chunk:
    return Chunk(
        id=identifier, doc_id=identifier, text=text, page=1, char_start=0, char_end=len(text)
    )


def test_unicode_tokenization_and_stable_term_ids() -> None:
    assert tokenize("ＲＥＰＯＲＴ, Café cafe\u0301; विभाग रिपोर्ट 123.") == [
        "report",
        "café",
        "café",
        "विभाग",
        "रिपोर्ट",
        "123",
    ]
    assert term_id("hello") == 754077114
    assert Bm25Encoder().document("Report विभाग") == Bm25Encoder().document("report विभाग")


def test_tf_saturation_length_penalty_and_binary_query() -> None:
    encoder = Bm25Encoder(avg_doc_length=4)
    vector = encoder.document("apple apple pear pear")
    assert vector["indices"] == sorted([term_id("apple"), term_id("pear")])
    assert vector["values"] == pytest.approx([1.375, 1.375])
    assert encoder.query("APPLE apple pear")["values"] == [1.0, 1.0]
    short = encoder.document("apple")
    long = encoder.document("apple pear pear pear pear")
    assert (
        short["values"][short["indices"].index(term_id("apple"))]
        > (long["values"][long["indices"].index(term_id("apple"))])
    )
    saturated = Bm25Encoder(b=0).document("apple apple apple")["values"][0]
    assert 1 < saturated < 3
    assert encoder.document("! \n") == encoder.query("") == {"indices": [], "values": []}


def test_hash_collisions_aggregate_before_weighting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("intellidocs_intelligence.sparse.term_id", lambda _term: 7)
    vector = Bm25Encoder(avg_doc_length=2).document("different terms")
    assert vector == {"indices": [7], "values": pytest.approx([1.375])}
    assert Bm25Encoder().query("different terms") == {"indices": [7], "values": [1.0]}


@pytest.mark.parametrize(
    "name,value",
    [
        ("BM25_K1", "0"),
        ("BM25_K1", "nan"),
        ("BM25_B", "1.1"),
        ("BM25_B", "-0.1"),
        ("BM25_AVG_DOC_LENGTH", "0"),
        ("BM25_AVG_DOC_LENGTH", "inf"),
        ("BM25_K1", "invalid"),
    ],
)
def test_invalid_settings(name: str, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        embedder_from_env()


def test_e5_default_bm25_uses_raw_text_and_distinct_query_weights(
    stub_model: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoder = Bm25Encoder()
    embedder = embedder_from_env()
    assert isinstance(embedder, SparseOverrideEmbedder) and embedder.sparse_requires_idf
    document = embedder.embed(["Report report"])[0]
    query = embedder.embed_queries(["Report report"])[0]
    assert document["dense"] == query["dense"] == [1.0, 0.0]
    assert document["sparse"] == encoder.document("Report report")
    assert query["sparse"] == encoder.query("Report report")
    assert stub_model == [(["Report report"], "passage: "), (["Report report"], "query: ")]
    assert document["sparse"]["indices"] == [term_id("report")]
    assert embedder.embed(["other", "Report report"])[1] == document  # No batch statistics.
    monkeypatch.setenv("SPARSE_EMBEDDING_BACKEND", "none")
    disabled = embedder_from_env()
    assert disabled.embed(["Report report"])[0]["sparse"] == {"indices": [], "values": []}
    assert disabled.cache_key() != embedder.cache_key()
    monkeypatch.setenv("SPARSE_EMBEDDING_BACKEND", "native")
    with pytest.raises(ValueError, match="no native sparse"):
        embedder_from_env()
    monkeypatch.setenv("SPARSE_EMBEDDING_BACKEND", "unknown")
    with pytest.raises(ValueError, match="Unsupported SPARSE"):
        embedder_from_env()


def test_native_bge_preserved_and_bm25_override_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_BACKEND", "bge_m3")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    native = {"dense": [1.0, 0.0], "sparse": {"indices": [11], "values": [0.8]}}
    monkeypatch.setattr(BgeM3Embedder, "embed", lambda _self, texts: [native for _ in texts])
    embedder = embedder_from_env()
    assert isinstance(embedder, BgeM3Embedder) and not embedder.sparse_requires_idf
    assert embedder.embed(["report"]) == [native]
    assert embedder.embed_queries(["report"]) == [native]
    monkeypatch.setenv("SPARSE_EMBEDDING_BACKEND", "bm25")
    overridden = embedder_from_env()
    assert overridden.embed(["report"])[0] == {
        "dense": [1.0, 0.0],
        "sparse": Bm25Encoder().document("report"),
    }


def test_adapter_pool_and_embedding_fingerprint_include_sparse_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cached_embedder.cache_clear()
    try:
        first = _cached_embedder(embedding_settings_key())
        monkeypatch.setenv("BM25_K1", "2.0")
        changed = _cached_embedder(embedding_settings_key())
        assert changed is not first and changed.cache_key() != first.cache_key()
        monkeypatch.setenv("EMBEDDING_QUERY_PROMPT", "search: ")
        query_changed = _cached_embedder(embedding_settings_key())
        assert query_changed is not changed
        assert query_changed.cache_key() == changed.cache_key()  # Document vectors are unchanged.
    finally:
        _cached_embedder.cache_clear()


def test_qdrant_idf_scoring_and_hybrid_queries(
    client: QdrantClient,
    stub_model: list,
) -> None:
    embedder = embedder_from_env()
    store = QdrantStore(client, "bm25", embedder)
    docs = [
        chunk("common1", "common common"),
        chunk("common2", "common"),
        chunk("rare", "rare"),
        chunk("hindi", "विभाग रिपोर्ट"),
    ]
    store.upsert_chunks(docs)
    assert client.get_collection("bm25").config.params.sparse_vectors["sparse"].modifier == (
        models.Modifier.IDF
    )
    for text, expected in (("rare", "rare"), ("रिपोर्ट", "hindi")):
        query = embedder.embed_queries([text])[0]
        sparse = models.SparseVector(**query["sparse"])
        matches = client.query_points("bm25", query=sparse, using="sparse", limit=4).points
        assert matches[0].payload["doc_id"] == expected and matches[0].score > 0
        combined = client.query_points(
            "bm25",
            prefetch=[
                models.Prefetch(query=query["dense"], using="dense", limit=4),
                models.Prefetch(query=sparse, using="sparse", limit=4),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=4,
        ).points
        assert combined[0].payload["doc_id"] == expected
    rare_query = models.SparseVector(**Bm25Encoder().query("rare"))
    before = client.query_points("bm25", query=rare_query, using="sparse").points[0].score
    doc_weight = Bm25Encoder().document("rare")["values"][0]
    assert before == pytest.approx(doc_weight * math.log(1 + (4 - 1 + 0.5) / (1 + 0.5)))
    store.upsert_chunks(docs)
    assert client.query_points("bm25", query=rare_query, using="sparse").points[0].score == (
        pytest.approx(before)
    )
    store.delete_document("common1")
    after = client.query_points("bm25", query=rare_query, using="sparse").points[0].score
    assert after == pytest.approx(doc_weight * math.log(1 + (3 - 1 + 0.5) / (1 + 0.5)))


def test_incompatible_idf_collection_is_rejected_before_upsert(client: QdrantClient) -> None:
    native = FakeEmbedder()
    bm25 = SparseOverrideEmbedder(native, Bm25Encoder())
    QdrantStore(client, "native", native).upsert_chunks([chunk("old", "old")])
    with pytest.raises(ValueError, match="sparse IDF modifier"):
        QdrantStore(client, "native", bm25).upsert_chunks([chunk("new", "new")])
    assert client.count("native").count == 1
    QdrantStore(client, "bm25", bm25).upsert_chunks([chunk("old", "old")])
    with pytest.raises(ValueError, match="sparse IDF modifier"):
        QdrantStore(client, "bm25", native).upsert_chunks([chunk("new", "new")])
    assert client.count("bm25").count == 1


@pytest.mark.parametrize("policy", ["auto", "bm25"])
def test_bge_hybrid_queries_with_native_or_bm25_sparse(
    client: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    monkeypatch.setenv("EMBEDDING_BACKEND", "bge_m3")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("SPARSE_EMBEDDING_BACKEND", policy)

    def native(_self: BgeM3Embedder, texts: list[str]) -> list[Embedding]:
        return [
            {
                "dense": [1.0, 0.0],
                "sparse": {"indices": [11 if text == "rare" else 12], "values": [0.8]},
            }
            for text in texts
        ]

    monkeypatch.setattr(BgeM3Embedder, "embed", native)
    embedder = embedder_from_env()
    store = QdrantStore(client, "bge", embedder)
    store.upsert_chunks(
        [chunk("common1", "common"), chunk("common2", "common"), chunk("match", "rare")]
    )
    query = embedder.embed_queries(["rare"])[0]
    matches = client.query_points(
        "bge",
        prefetch=[
            models.Prefetch(query=query["dense"], using="dense", limit=3),
            models.Prefetch(query=models.SparseVector(**query["sparse"]), using="sparse", limit=3),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=3,
    ).points
    assert matches[0].payload["doc_id"] == "match"
    modifier = client.get_collection("bge").config.params.sparse_vectors["sparse"].modifier
    assert modifier == (models.Modifier.IDF if policy == "bm25" else models.Modifier.NONE)
