"""Compose retrieval, reranking, generation, and verified answer assembly."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from intellidocs_intelligence.models import (
    Answer,
    AnswerRequest,
    Chunk,
    ParsedDocument,
    SearchRequest,
)

from .generation import ClaimGenerator, GeneratedClaim, GenerationError, generator_from_env
from .search import SearchService, search_from_env
from .storage import Storage
from .verification import CitationVerifier, verifier_from_env

AnswerFactory = Callable[[], AbstractContextManager["AnswerService"]]


@dataclass(frozen=True)
class AnswerDraft:
    """Internal evidence and drafts for paired offline evaluation; never an HTTP response."""

    chunks: list[Chunk]
    claims: list[GeneratedClaim]
    documents: dict[str, ParsedDocument] | None
    generation_failed: bool = False


class AnswerService:
    def __init__(
        self,
        search: SearchService,
        generator: ClaimGenerator,
        verifier: CitationVerifier,
        storage: Storage | None = None,
    ) -> None:
        self.search = search
        self.generator = generator
        self.verifier = verifier
        self.storage = storage

    def prepare(self, request: AnswerRequest) -> AnswerDraft:
        results = self.search.search(
            SearchRequest(query=request.query, filters=request.filters, top_k=request.top_k)
        )
        chunks = [result.chunk for result in results.results]
        documents = None
        if self.storage is not None:
            documents = {}
            org_id = (request.filters or {}).get("org_id")
            for doc_id in dict.fromkeys(chunk.doc_id for chunk in chunks):
                try:
                    parsed = self.storage.load_result(doc_id)
                    if parsed.org_id == org_id:
                        documents[doc_id] = parsed.document
                except (OSError, ValueError):
                    # Missing/stale source material cannot yield verified citations.
                    continue
        try:
            generated = self.generator.generate(request.query, chunks, language=request.language)
        except GenerationError:
            return AnswerDraft(chunks, [], documents, generation_failed=True)
        return AnswerDraft(chunks, generated.claims, documents)

    def finalize(self, draft: AnswerDraft) -> Answer:
        answer = self.verifier.build_answer(
            draft.claims,
            draft.chunks,
            model=self.generator.provider.model,
            documents=draft.documents,
        )
        if draft.generation_failed:
            answer.abstain_reason = "Generation did not produce a valid claims response."
        return answer

    def answer(self, request: AnswerRequest) -> Answer:
        return self.finalize(self.prepare(request))


@contextmanager
def answerer_from_env(storage: Storage) -> Iterator[AnswerService]:
    with search_from_env() as search:
        yield AnswerService(search, generator_from_env(), verifier_from_env(), storage)
