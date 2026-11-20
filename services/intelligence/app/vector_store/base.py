"""Backend-independent vector store operations."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

from intellidocs_intelligence.embeddings import Embedding
from intellidocs_intelligence.models import Chunk, SearchResult


class VectorStore(ABC):
    @abstractmethod
    def upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Embedding] | None = None,
        *,
        document_title: str | None = None,
    ) -> int:
        """Persist embedded chunks and return the acknowledged upsert count."""

    @abstractmethod
    def delete_document(
        self, doc_id: str, *, keep_chunk_ids: Sequence[str] = (), org_id: str | None = None
    ) -> None:
        """Remove document points except explicitly retained IDs, without embedding."""

    def search(
        self,
        query: Embedding,
        *,
        limit: int,
        filters: Mapping[str, object] | None = None,
    ) -> list[SearchResult]:
        """Reserved seam for retrieval; no ranking/search implementation yet."""
        raise NotImplementedError("Vector search is not implemented")
