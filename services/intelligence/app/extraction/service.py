"""Extract obligations from the acknowledged index for a stored document."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from uuid import UUID

from app.ingestion import load_org
from app.storage import Storage
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import configured_embedder
from intellidocs_intelligence.models import ExtractionResult, OrgConfig

from .extractor import ObligationExtractor, extractor_from_env
from .processor import ObligationProcessor

ExtractionFactory = Callable[[], AbstractContextManager["ExtractionService"]]


class IndexNotReady(ValueError):
    """The document does not have a complete acknowledged index."""


class ExtractionService:
    def __init__(
        self,
        storage: Storage,
        store: QdrantStore,
        create_extractor: Callable[[OrgConfig], ObligationExtractor] = extractor_from_env,
    ) -> None:
        self.storage = storage
        self.store = store
        self.create_extractor = create_extractor

    def extract(self, doc_id: str, *, org_id: str | None = None) -> ExtractionResult:
        try:
            identifier = UUID(doc_id)
        except ValueError as error:
            raise FileNotFoundError(doc_id) from error
        if str(identifier) != doc_id:
            raise FileNotFoundError(doc_id)
        # Read a consistent snapshot without holding the indexing lock during LLM calls.
        with self.storage.index_lock(doc_id):
            parsed = self.storage.load_result(doc_id)
            if org_id is not None and parsed.org_id != org_id:
                raise FileNotFoundError(doc_id)
            state = self.storage.load_index_cache(doc_id)
            if parsed.document.status != "ready" or not state or state.get("status") != "indexed":
                raise IndexNotReady("Document must be indexed before extraction")
            chunks = self.store.load_document_chunks(doc_id, org_id=parsed.org_id)
            if len(chunks) != state.get("chunk_count"):
                raise IndexNotReady("Stored index is incomplete; re-index the document")
        org = load_org(parsed.org_id)
        drafts = self.create_extractor(org).extract(chunks) if chunks else []
        return ObligationProcessor(org).process(parsed.document, chunks, drafts)


@contextmanager
def extraction_from_env(storage: Storage) -> Iterator[ExtractionService]:
    store = QdrantStore.from_env(configured_embedder())
    try:
        yield ExtractionService(storage, store)
    finally:
        store.client.close()
