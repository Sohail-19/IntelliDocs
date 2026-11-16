"""Retrieve, rerank, and cite indexed chunks."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from functools import lru_cache
from time import perf_counter

from intellidocs_intelligence.embeddings import configured_embedder
from intellidocs_intelligence.models import (
    Citation,
    SearchDebug,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchTimings,
)

from .reranking import Reranker, RerankerConfig, create_reranker
from .retrieval import Candidate, Retriever, create_retriever
from .vector_store import QdrantStore

SearchFactory = Callable[[], AbstractContextManager["SearchService"]]


def _search_result(candidate: Candidate) -> SearchResult:
    chunk = candidate.chunk
    page_start = chunk.page_start or chunk.page
    return SearchResult(
        chunk=chunk,
        score=candidate.score,
        rerank_score=candidate.rerank_score,
        source=candidate.source,
        citations=[
            Citation(
                doc_id=chunk.doc_id,
                page=page_start,
                page_start=page_start,
                page_end=chunk.page_end or page_start,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                quote=chunk.text,
                clause_number=chunk.clause_number,
                heading_path=chunk.heading_path or [],
            )
        ],
    )


class SearchService:
    def __init__(self, retriever: Retriever, reranker: Reranker) -> None:
        self.retriever = retriever
        self.reranker = reranker

    def search(self, request: SearchRequest) -> SearchResponse:
        start = perf_counter()
        # A final top_k must not cut off the pool before the reranker sees it.
        candidates = self.retriever.retrieve_pool(request.query, request.filters)
        retrieved = perf_counter()
        ranked = self.reranker.rerank(request.query, candidates, request.top_k)
        reranked = perf_counter()
        results = [_search_result(candidate) for candidate in ranked]
        debug = None
        if request.debug:
            debug = SearchDebug(
                retriever=type(self.retriever).__name__,
                reranker=type(self.reranker).__name__,
                timings_ms=SearchTimings(
                    retrieve=(retrieved - start) * 1_000,
                    rerank=(reranked - retrieved) * 1_000,
                    citations=(perf_counter() - reranked) * 1_000,
                ),
            )
        return SearchResponse(results=results, debug=debug)


@lru_cache(maxsize=4)
def _cached_reranker(config: RerankerConfig) -> Reranker:
    return create_reranker(config)


@contextmanager
def search_from_env() -> Iterator[SearchService]:
    store = QdrantStore.from_env(configured_embedder())
    try:
        yield SearchService(create_retriever(store), _cached_reranker(RerankerConfig.from_env()))
    finally:
        store.client.close()
