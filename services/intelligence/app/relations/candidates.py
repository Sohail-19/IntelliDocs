"""Find a bounded set of earlier documents using indexed evidence and retrieval."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from typing import Literal

from qdrant_client import models

from app.retrieval import RetrievalConfig, create_retriever
from app.vector_store import QdrantStore
from app.vector_store.qdrant_store import chunk_from_payload
from intellidocs_intelligence.models import Chunk, OrgConfig

from .signals import RELATION_KEYS_FIELD, indexed_keys, normalize, reference_keys, structure_keys

Reason = Literal["explicit_reference", "shared_clause", "shared_heading", "similarity"]


@dataclass(frozen=True)
class CandidateFinderConfig:
    candidate_limit: int = 10
    query_chunks: int = 24
    chunk_hits: int = 12
    min_similarity: float = 0.0

    def __post_init__(self) -> None:
        for name in ("candidate_limit", "query_chunks", "chunk_hits"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.chunk_hits > 100:
            raise ValueError("chunk_hits must not exceed 100")
        if not math.isfinite(self.min_similarity):
            raise ValueError("min_similarity must be finite")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CandidateFinderConfig:
        settings = os.environ if env is None else env
        return cls(
            candidate_limit=int(settings.get("RELATION_CANDIDATE_LIMIT", "10")),
            query_chunks=int(settings.get("RELATION_QUERY_CHUNKS", "24")),
            chunk_hits=int(settings.get("RELATION_CHUNK_HITS", "12")),
            min_similarity=float(settings.get("RELATION_MIN_SIMILARITY", "0")),
        )


@dataclass(frozen=True)
class ChunkPair:
    source_chunk: Chunk
    target_chunk: Chunk
    reasons: tuple[Reason, ...]
    evidence_keys: tuple[str, ...]
    needs_date_review: bool
    similarity_score: float | None


@dataclass(frozen=True)
class RelationCandidate:
    target_doc_id: str
    matching_pairs: tuple[ChunkPair, ...]

    @property
    def reasons(self) -> tuple[Reason, ...]:
        return tuple(sorted({reason for pair in self.matching_pairs for reason in pair.reasons}))

    @property
    def needs_date_review(self) -> bool:
        return any(pair.needs_date_review for pair in self.matching_pairs)


def _prior_documents(source: Chunk, org_id: str) -> models.Filter:
    conditions: list[models.Condition] = [
        models.FieldCondition(key="org_id", match=models.MatchValue(value=org_id))
    ]
    if source.doc_date is not None:
        conditions.append(
            models.Filter(
                should=[
                    models.FieldCondition(
                        key="doc_date",
                        range=models.DatetimeRange(
                            lt=datetime.combine(source.doc_date, time.min, UTC)
                        ),
                    ),
                    models.IsEmptyCondition(is_empty=models.PayloadField(key="doc_date")),
                ]
            )
        )
    return models.Filter(
        must=conditions,
        must_not=[
            models.FieldCondition(key="doc_id", match=models.MatchValue(value=source.doc_id))
        ],
    )


def _reasons(evidence: set[str], score: float | None) -> tuple[Reason, ...]:
    reasons: list[Reason] = []
    if any(key.startswith(("number:", "date:", "title:")) for key in evidence):
        reasons.append("explicit_reference")
    if any(key.startswith("clause:") for key in evidence):
        reasons.append("shared_clause")
    if any(key.startswith("heading:") for key in evidence):
        reasons.append("shared_heading")
    if score is not None:
        reasons.append("similarity")
    return tuple(reasons)


def _pair_order(pair: ChunkPair) -> tuple[bool, bool, bool, float, int, str]:
    return (
        "explicit_reference" not in pair.reasons,
        not any(reason in pair.reasons for reason in ("shared_clause", "shared_heading")),
        pair.needs_date_review,
        -pair.similarity_score if pair.similarity_score is not None else math.inf,
        pair.source_chunk.char_start,
        pair.target_chunk.id,
    )


class CandidateFinder:
    def __init__(
        self,
        store: QdrantStore,
        org: OrgConfig,
        config: CandidateFinderConfig | None = None,
        retrieval: RetrievalConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config if config is not None else CandidateFinderConfig.from_env()
        self.retriever = create_retriever(store, retrieval)
        rules = org.relation_detection
        languages = {language.casefold() for language in org.languages}
        self.cues: dict[str, tuple[str, ...]] = {}
        for language in rules.reference_cues.keys() | rules.change_cues.keys():
            if language.casefold() not in languages:
                raise ValueError(f"Reference cue language {language!r} is not in languages")
            cues = [
                *rules.reference_cues.get(language, []),
                *(
                    phrase
                    for phrases in rules.change_cues.get(language, {}).values()
                    for phrase in phrases
                ),
            ]
            self.cues[language.casefold()] = tuple(
                dict.fromkeys(" ".join(normalize(cue).split()) for cue in cues)
            )
        self.related = rules.related_document_types
        types = set(org.document_types)
        if any(
            source not in types or not types.issuperset(targets)
            for source, targets in self.related.items()
        ):
            raise ValueError("Related document types must be listed in document_types")

    def _references(self, chunk: Chunk) -> set[str]:
        if chunk.language is None:
            return set()
        code = chunk.language.casefold()
        if code not in self.cues:
            code = code.split("-", maxsplit=1)[0]
        cues = self.cues.get(code)
        return reference_keys(chunk, cues, code) if cues else set()

    def _exact_matches(self, keys: set[str], where: models.Filter) -> list[tuple[Chunk, set[str]]]:
        match = models.FieldCondition(
            key=RELATION_KEYS_FIELD, match=models.MatchAny(any=sorted(keys))
        )
        result = self.store.client.query_points_groups(
            collection_name=self.store.collection_name,
            group_by="doc_id",
            query_filter=models.Filter(must=[where, match]),
            limit=max(self.config.candidate_limit, self.config.chunk_hits),
            group_size=1,
            with_payload=True,
            with_vectors=False,
        )
        matches = []
        for group in result.groups:
            for hit in group.hits:
                evidence = (hit.payload or {}).get(RELATION_KEYS_FIELD, [])
                matches.append((chunk_from_payload(hit.payload), keys.intersection(evidence)))
        return matches

    def find(self, doc_id: str, *, org_id: str) -> list[RelationCandidate]:
        if not doc_id.strip() or not org_id.strip():
            raise ValueError("A document ID and organization ID are required")
        chunks = self.store.load_document_chunks(doc_id, org_id=org_id)
        return self.find_for_chunks(doc_id, chunks, org_id=org_id)

    def find_for_chunks(
        self, doc_id: str, chunks: Sequence[Chunk], *, org_id: str
    ) -> list[RelationCandidate]:
        """Use an already-read source snapshot with the same tenant restrictions."""
        if any(chunk.doc_id != doc_id or chunk.org_id != org_id for chunk in chunks):
            raise ValueError("Source chunks must belong to the requested document and organization")
        if not chunks:
            return []
        where = _prior_documents(chunks[0], org_id)
        doc_type = chunks[0].doc_type
        similarity_filter = (
            models.Filter(
                must=[
                    where,
                    models.FieldCondition(
                        key="doc_type",
                        match=models.MatchAny(any=[doc_type, *self.related.get(doc_type, [])]),
                    ),
                ]
            )
            if doc_type
            else None
        )
        # Give explicit references first access to the query budget regardless of
        # where they appear in the source document.
        sources = [(chunk, self._references(chunk), structure_keys(chunk)) for chunk in chunks]
        sources.sort(key=lambda item: (not item[1], not item[2]))

        pairs: dict[str, dict[tuple[str, str], ChunkPair]] = {}

        def record(source: Chunk, target: Chunk, evidence: set[str], score: float | None) -> None:
            if target.org_id != org_id or target.doc_id == doc_id:
                return
            needs_review = source.doc_date is None or target.doc_date is None
            if source.doc_date is not None and target.doc_date is not None:
                if target.doc_date >= source.doc_date:
                    return
            reasons = _reasons(evidence, score)
            if not reasons:
                return
            matches = pairs.setdefault(target.doc_id, {})
            key = (source.id, target.id)
            pair = matches.get(key)
            if pair is None:
                matches[key] = ChunkPair(
                    source, target, reasons, tuple(sorted(evidence)), needs_review, score
                )
            else:
                merged_evidence = evidence.union(pair.evidence_keys)
                merged_score = score if score is not None else pair.similarity_score
                matches[key] = replace(
                    pair,
                    evidence_keys=tuple(sorted(merged_evidence)),
                    reasons=_reasons(merged_evidence, merged_score),
                    similarity_score=merged_score,
                )

        for source, references, structure in sources[: self.config.query_chunks]:
            for keys in (references, structure):
                if keys:
                    for target, evidence in self._exact_matches(keys, where):
                        record(source, target, evidence, None)
            if similarity_filter is None or not source.text.strip():
                continue
            for hit in self.retriever.retrieve_matching(
                source.text, similarity_filter, self.config.chunk_hits
            ):
                # Reuse legacy payloads when a similarity hit predates exact keys.
                evidence = (references | structure).intersection(indexed_keys(hit.chunk))
                score = hit.score if hit.score > self.config.min_similarity else None
                record(source, hit.chunk, evidence, score)

        candidates = [
            RelationCandidate(target, tuple(sorted(matches.values(), key=_pair_order)))
            for target, matches in pairs.items()
        ]
        candidates.sort(key=lambda item: (_pair_order(item.matching_pairs[0]), item.target_doc_id))
        return candidates[: self.config.candidate_limit]
