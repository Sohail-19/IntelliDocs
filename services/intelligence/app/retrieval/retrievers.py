"""Chunk retrieval over the named Qdrant vectors used by the indexer."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from qdrant_client import models

from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import Embedding
from intellidocs_intelligence.models import Chunk

from .config import RetrievalConfig, validate_top_k
from .filters import payload_filter

Source = Literal["dense", "sparse", "fused"]
Vector = list[float] | models.SparseVector

# Qdrant 1.12 exposes this constant through Fusion.RRF, with zero-based ranks.
QDRANT_RRF_K = 2


@dataclass(frozen=True)
class Candidate:
    chunk: Chunk
    score: float
    source: Source
    rerank_score: float | None = None


class Retriever(ABC):
    @abstractmethod
    def retrieve(
        self, query: str, filters: Mapping[str, str] | None = None, top_k: int | None = None
    ) -> list[Candidate]:
        """Return ranked chunks for the supplied query."""

    @abstractmethod
    def retrieve_pool(
        self, query: str, filters: Mapping[str, str] | None = None
    ) -> list[Candidate]:
        """Return the configured candidate pool before final result limiting."""


def _point_order(point: models.ScoredPoint) -> tuple[float, str]:
    return -point.score, str(point.id)


class _QdrantRetriever(Retriever):
    source: Source

    def __init__(self, store: QdrantStore, config: RetrievalConfig | None = None) -> None:
        self.store = store
        self.config = config if config is not None else RetrievalConfig.from_env()

    def retrieve(
        self, query: str, filters: Mapping[str, str] | None = None, top_k: int | None = None
    ) -> list[Candidate]:
        limit = self.config.default_top_k if top_k is None else validate_top_k(top_k)
        return self.retrieve_pool(query, filters)[:limit]

    def retrieve_pool(
        self, query: str, filters: Mapping[str, str] | None = None
    ) -> list[Candidate]:
        if not query.strip():
            raise ValueError("Retrieval query must not be blank")
        where = payload_filter(filters)
        if not self.store.client.collection_exists(self.store.collection_name):
            return []
        embeddings = self.store.embedder.embed_queries([query])
        if len(embeddings) != 1:
            raise ValueError("Expected one query embedding")
        points = self._search(embeddings[0], where, self.config.candidate_pool_size)
        return [
            Candidate(Chunk.model_validate(point.payload), point.score, self.source)
            for point in sorted(points, key=_point_order)
        ]

    def _query(
        self, vector: Vector, name: str, where: models.Filter | None, pool: int
    ) -> list[models.ScoredPoint]:
        return self.store.client.query_points(
            collection_name=self.store.collection_name,
            query=vector,
            using=name,
            query_filter=where,
            limit=pool,
            with_payload=True,
            with_vectors=False,
        ).points

    @abstractmethod
    def _search(
        self, embedding: Embedding, where: models.Filter | None, pool: int
    ) -> list[models.ScoredPoint]:
        """Fetch the bounded candidate set."""


class DenseOnlyRetriever(_QdrantRetriever):
    source: Source = "dense"

    def _search(
        self, embedding: Embedding, where: models.Filter | None, pool: int
    ) -> list[models.ScoredPoint]:
        return self._query(embedding["dense"], self.store.dense_vector_name, where, pool)


class SparseOnlyRetriever(_QdrantRetriever):
    source: Source = "sparse"

    def _search(
        self, embedding: Embedding, where: models.Filter | None, pool: int
    ) -> list[models.ScoredPoint]:
        if not embedding["sparse"]["indices"]:
            return []
        vector = models.SparseVector(**embedding["sparse"])
        return self._query(vector, self.store.sparse_vector_name, where, pool)


class HybridRetriever(_QdrantRetriever):
    source: Source = "fused"

    def _search(
        self, embedding: Embedding, where: models.Filter | None, pool: int
    ) -> list[models.ScoredPoint]:
        branches: list[tuple[str, Vector]] = [(self.store.dense_vector_name, embedding["dense"])]
        if embedding["sparse"]["indices"]:
            branches.append(
                (self.store.sparse_vector_name, models.SparseVector(**embedding["sparse"]))
            )

        if self.config.rrf_k == QDRANT_RRF_K:
            return self.store.client.query_points(
                collection_name=self.store.collection_name,
                prefetch=[
                    models.Prefetch(query=vector, using=name, filter=where, limit=pool)
                    for name, vector in branches
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                # Fetch the whole bounded union before resolving fused score ties.
                limit=pool * len(branches),
                with_payload=True,
                with_vectors=False,
            ).points

        # Parameterized server-side RRF requires Qdrant 1.16. On the pinned
        # version, retrieve each branch once and apply the configured k here.
        scores: dict[models.ExtendedPointId, float] = {}
        points: dict[models.ExtendedPointId, models.ScoredPoint] = {}
        for name, vector in branches:
            branch = sorted(self._query(vector, name, where, pool), key=_point_order)
            for rank, point in enumerate(branch):
                scores[point.id] = scores.get(point.id, 0.0) + 1 / (self.config.rrf_k + rank)
                points[point.id] = point
        return [point.model_copy(update={"score": scores[key]}) for key, point in points.items()]


def create_retriever(store: QdrantStore, config: RetrievalConfig | None = None) -> Retriever:
    settings = config if config is not None else RetrievalConfig.from_env()
    implementations: dict[str, type[_QdrantRetriever]] = {
        "hybrid": HybridRetriever,
        "dense": DenseOnlyRetriever,
        "sparse": SparseOnlyRetriever,
    }
    return implementations[settings.mode](store, settings)
