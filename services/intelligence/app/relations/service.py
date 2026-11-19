"""Run relation detection against stored and indexed documents."""

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from itertools import islice
from pathlib import Path
from uuid import UUID

from app.extraction.service import IndexNotReady
from app.ingestion import load_org
from app.storage import Storage
from app.vector_store import QdrantStore
from intellidocs_intelligence.cache import DiskCache
from intellidocs_intelligence.embeddings import configured_embedder
from intellidocs_intelligence.llm import provider_from_env
from intellidocs_intelligence.models import (
    DocumentRelation,
    OrgConfig,
    ParsedDocument,
    RelationResult,
)

from .candidates import CandidateFinder
from .classifier import ClassificationError, RelationClassifier

RelationFactory = Callable[[], AbstractContextManager["RelationService"]]


def classifier_from_env(org: OrgConfig) -> RelationClassifier:
    storage = Path(os.environ.get("STORAGE_DIR", "data/uploads"))
    cache = DiskCache(Path(os.environ.get("LLM_CACHE_DIR") or storage / "llm-cache"))
    return RelationClassifier(
        provider_from_env(),
        org,
        max_retries=int(os.environ.get("RELATION_MAX_RETRIES", "1")),
        cache=cache,
    )


class RelationService:
    def __init__(
        self,
        storage: Storage,
        store: QdrantStore,
        create_classifier: Callable[[OrgConfig], RelationClassifier] = classifier_from_env,
        *,
        max_pairs: int = 10,
        pairs_per_candidate: int = 2,
    ) -> None:
        if type(max_pairs) is not int or max_pairs < 0:
            raise ValueError("RELATION_MAX_PAIRS must be a nonnegative integer")
        if type(pairs_per_candidate) is not int or pairs_per_candidate < 1:
            raise ValueError("RELATION_PAIRS_PER_CANDIDATE must be a positive integer")
        self.storage = storage
        self.store = store
        self.create_classifier = create_classifier
        self.max_pairs = max_pairs
        self.pairs_per_candidate = pairs_per_candidate

    def detect(self, doc_id: str) -> RelationResult:
        try:
            if str(UUID(doc_id)) != doc_id:
                raise ValueError("Document ID must be canonical")
        except ValueError as error:
            raise FileNotFoundError(doc_id) from error
        with self.storage.index_lock(doc_id):
            parsed = self.storage.load_result(doc_id)
            state = self.storage.load_index_cache(doc_id)
            if parsed.document.status != "ready" or not state or state.get("status") != "indexed":
                raise IndexNotReady("Document must be indexed before detecting relations")
            org_id = parsed.org_id
            if org_id is None:
                raise IndexNotReady("Relation detection requires an indexed organization")
            chunks = self.store.load_document_chunks(doc_id, org_id=org_id)
            if len(chunks) != state.get("chunk_count"):
                raise IndexNotReady("Stored index is incomplete; re-index the document")

        if not chunks:
            return RelationResult(doc_id=doc_id, relations=[], warnings=[])
        org = load_org(org_id)
        candidates = CandidateFinder(self.store, org).find_for_chunks(doc_id, chunks, org_id=org_id)
        # Give each candidate a chance before processing another pair for the same document.
        pairs = (
            candidate.matching_pairs[index]
            for index in range(self.pairs_per_candidate)
            for candidate in candidates
            if index < len(candidate.matching_pairs)
        )
        selected = list(islice(pairs, self.max_pairs + 1))
        warnings: list[str] = []
        if len(selected) > self.max_pairs or any(
            len(candidate.matching_pairs) > self.pairs_per_candidate for candidate in candidates
        ):
            warnings.append("Some candidate pairs were skipped by the classification limits.")
        classifier = None
        targets: dict[str, ParsedDocument | None] = {}
        relations: dict[str, DocumentRelation] = {}
        for pair in selected[: self.max_pairs]:
            target_id = pair.target_chunk.doc_id
            if target_id not in targets:
                try:
                    target = self.storage.load_result(target_id)
                    targets[target_id] = (
                        target.document
                        if target.org_id == org_id and target.document.status == "ready"
                        else None
                    )
                except (OSError, ValueError):
                    targets[target_id] = None
            document = targets[target_id]
            if document is None:
                warnings.append(f"Source evidence is unavailable for target {target_id}.")
                continue
            if classifier is None:
                classifier = self.create_classifier(org)
            try:
                relation = classifier.classify(
                    pair, documents={doc_id: parsed.document, target_id: document}
                )
            except ClassificationError:
                warnings.append(
                    f"A candidate pair for {target_id} returned invalid classification output."
                )
                continue
            if relation is not None:
                previous = relations.get(relation.id)
                if previous is None or (not relation.needs_review, relation.confidence) > (
                    not previous.needs_review,
                    previous.confidence,
                ):
                    relations[relation.id] = relation
        return RelationResult(
            doc_id=doc_id,
            relations=list(relations.values()),
            warnings=list(dict.fromkeys(warnings)),
        )


@contextmanager
def relations_from_env(storage: Storage) -> Iterator[RelationService]:
    store = QdrantStore.from_env(configured_embedder())
    try:
        yield RelationService(
            storage,
            store,
            max_pairs=int(os.environ.get("RELATION_MAX_PAIRS", "10")),
            pairs_per_candidate=int(os.environ.get("RELATION_PAIRS_PER_CANDIDATE", "2")),
        )
    finally:
        store.client.close()
