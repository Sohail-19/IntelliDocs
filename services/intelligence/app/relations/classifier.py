"""Classify chunk pairs and retain only verified citations in proposed relations."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from string import Template
from typing import Any, Self, TypeVar
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from app.chunking.chunker import sentence_spans
from app.extraction.detector import contains_phrase
from app.generation import GeneratedClaim
from app.verification import CitationVerifier, VerificationConfig
from app.verification.matching import normalize as normalize_quote
from intellidocs_intelligence.cache import CachedLLMProvider, DiskCache
from intellidocs_intelligence.llm import LLMProvider
from intellidocs_intelligence.models import (
    ChangeRelationType,
    Chunk,
    Citation,
    DocumentRelation,
    DocumentRelationType,
    OrgConfig,
    ParsedDocument,
    RelationConfidenceBreakdown,
    RelationScope,
)

from .candidates import ChunkPair
from .signals import normalize

CLASSIFIER_VERSION = "relation-classifier.v1"
PROMPTS = Path(__file__).with_name("prompts")
T = TypeVar("T")


class RelationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    relation_type: DocumentRelationType | None
    scope: RelationScope | None
    source_quote: str
    target_quote: str

    @model_validator(mode="after")
    def check_evidence_fields(self) -> Self:
        if self.relation_type is None:
            if self.scope is not None or self.source_quote or self.target_quote:
                raise ValueError("An empty result requires a null scope and empty quotes")
        elif self.scope is None or not self.source_quote.strip() or not self.target_quote.strip():
            raise ValueError("A relation requires a scope and two nonempty quotes")
        return self


class ClassificationError(RuntimeError):
    """The provider or cache did not yield a valid draft after the retry limit."""


class _ValidatedProvider(LLMProvider):
    def __init__(self, provider: LLMProvider, max_retries: int) -> None:
        self.provider = provider
        self.provider_id = provider.provider_id
        self.model = provider.model
        self.max_retries = max_retries

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        original = prompt
        for attempt in range(self.max_retries + 1):
            try:
                return RelationDraft.model_validate(
                    self.provider.generate(prompt, schema)
                ).model_dump(mode="json")
            except ValueError:
                if attempt == self.max_retries:
                    raise ClassificationError("Invalid relation output after retries") from None
                prompt = Template((PROMPTS / "retry.txt").read_text(encoding="utf-8")).substitute(
                    original_prompt=original, attempt=str(attempt + 2)
                )
        raise AssertionError("Relation retry loop exited unexpectedly")


def _passage(chunk: Chunk) -> dict[str, object]:
    return {
        "text": chunk.text,
        "clause_number": chunk.clause_number,
        "heading_path": chunk.heading_path,
    }


def _in_text(text: str, cues: Sequence[str]) -> bool:
    normalized = " ".join(normalize(text).split())
    return any(
        contains_phrase(normalized, phrase)
        for cue in cues
        if (phrase := " ".join(normalize(cue).split()))
    )


def _localized(rules: Mapping[str, T], language: str | None, default: T) -> T:
    code = (language or "").casefold()
    return rules.get(code, rules.get(code.split("-", 1)[0], default))


def _document_date(chunk: Chunk, documents: Mapping[str, ParsedDocument] | None) -> date | None:
    if documents is None:
        return chunk.doc_date
    document = documents.get(chunk.doc_id)
    return document.doc_date if document is not None else None


def _span(citation: Citation | None, chunk: Chunk) -> list[int] | str:
    if citation is None:
        return chunk.chunk_id or chunk.id
    return [citation.char_start, citation.char_end]


def _quote_context(chunk: Chunk, citation: Citation | None, abbreviations: Sequence[str]) -> str:
    if citation is None:
        return ""
    start = citation.char_start - chunk.char_start
    end = citation.char_end - chunk.char_start
    return " ".join(
        chunk.text[left:right]
        for left, right in sentence_spans(chunk.text, abbreviations)
        if left < end and right > start
    )


class RelationClassifier:
    def __init__(
        self,
        provider: LLMProvider,
        org: OrgConfig,
        *,
        max_retries: int = 1,
        cache: DiskCache | None = None,
    ) -> None:
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("RELATION_MAX_RETRIES must be a nonnegative integer")
        validated = _ValidatedProvider(provider, max_retries)
        self.provider = CachedLLMProvider(validated, cache) if cache is not None else validated
        self.verifier = CitationVerifier(VerificationConfig(fuzzy_threshold=1.0))
        self.abbreviations = org.document_structure.sentence_abbreviations
        rules = org.relation_detection
        self.change_cues = {key.casefold(): value for key, value in rules.change_cues.items()}
        self.reference_cues = {key.casefold(): value for key, value in rules.reference_cues.items()}
        self.nonoperative_cues = {
            key.casefold(): value for key, value in rules.nonoperative_cues.items()
        }
        self.directive_cues = {
            key.casefold(): value.cues for key, value in org.obligation_detection.items()
        }

    def _draft(self, source: Chunk, target: Chunk) -> RelationDraft:
        schema = RelationDraft.model_json_schema()
        prompt = Template((PROMPTS / "classify.txt").read_text(encoding="utf-8")).substitute(
            schema_json=json.dumps(schema),
            pair_json=json.dumps(
                {"NEW": _passage(source), "OLD": _passage(target)}, ensure_ascii=False
            ),
        )
        try:
            return RelationDraft.model_validate(self.provider.generate(prompt, schema))
        except ValueError:
            raise ClassificationError("Cached relation output is invalid") from None

    def classify(
        self, pair: ChunkPair, *, documents: Mapping[str, ParsedDocument] | None = None
    ) -> DocumentRelation | None:
        source, target = pair.source_chunk, pair.target_chunk
        if source.doc_id == target.doc_id or source.org_id != target.org_id:
            raise ValueError("Relation pairs require distinct documents in the same organization")
        draft = self._draft(source, target)
        kind, scope = draft.relation_type, draft.scope
        if kind is None or scope is None:
            return None

        claims = [
            GeneratedClaim(text=quote, quote=quote, chunk_id=chunk.chunk_id or chunk.id)
            for chunk, quote in ((source, draft.source_quote), (target, draft.target_quote))
        ]
        verified = self.verifier.verify(claims, [source, target], documents=documents)
        source_citation, target_citation = (
            claim.citations[0] if claim.status == "verified" and claim.citations else None
            for claim in verified
        )
        both_verified = source_citation is not None and target_citation is not None
        source_quote = source_citation.quote if source_citation else ""
        target_quote = target_citation.quote if target_citation else ""
        changes: dict[ChangeRelationType, list[str]] = _localized(
            self.change_cues, source.language, {}
        )
        blocked_cues = _localized(self.nonoperative_cues, source.language, [])
        source_sentences = (
            source.text[left:right]
            for left, right in sentence_spans(source.text, self.abbreviations)
        )
        has_operative_change = any(
            any(_in_text(sentence, cues) for cues in changes.values())
            and not _in_text(sentence, blocked_cues)
            for sentence in source_sentences
        )
        explicit_reference = "explicit_reference" in pair.reasons
        if kind == "conflicts_with":
            if both_verified and (
                has_operative_change
                or normalize_quote(source_quote).text == normalize_quote(target_quote).text
            ):
                return None
            recognized_cue = _in_text(
                source_quote, _localized(self.directive_cues, source.language, [])
            ) and _in_text(target_quote, _localized(self.directive_cues, target.language, []))
        elif kind == "references":
            recognized_cue = not has_operative_change and _in_text(
                source_quote, _localized(self.reference_cues, source.language, [])
            )
        else:
            recognized_cue = _in_text(source_quote, changes.get(kind, [])) and not _in_text(
                _quote_context(source, source_citation, self.abbreviations), blocked_cues
            )
            if (
                kind == "amends"
                and target.clause_number
                and _in_text(source_quote, [target.clause_number.rstrip(".")])
            ):
                scope = "clauses"

        source_date, target_date = (_document_date(chunk, documents) for chunk in (source, target))
        dates_consistent = (
            not pair.needs_date_review
            and source_date is not None
            and target_date is not None
            and target_date < source_date
        )
        breakdown = RelationConfidenceBreakdown(
            explicit_reference=0.20 if explicit_reference else 0.0,
            both_quotes_verified=0.45 if both_verified else 0.0,
            date_order_consistent=0.20 if dates_consistent else 0.0,
            recognized_cue=0.15 if recognized_cue else 0.0,
        )
        identity = [
            source.doc_id,
            target.doc_id,
            kind,
            scope,
            _span(source_citation, source),
            _span(target_citation, target),
        ]
        return DocumentRelation(
            id=str(uuid5(NAMESPACE_URL, json.dumps(identity))),
            source_doc_id=source.doc_id,
            target_doc_id=target.doc_id,
            relation_type=kind,
            scope=scope,
            source_citations=[source_citation] if source_citation else [],
            target_citations=[target_citation] if target_citation else [],
            confidence=breakdown.total,
            confidence_breakdown=breakdown,
            status="proposed",
            needs_review=(
                not both_verified
                or not dates_consistent
                or not recognized_cue
                or kind == "conflicts_with"
                or not explicit_reference
            ),
            detected_by=CLASSIFIER_VERSION,
            created_at=datetime.now(UTC),
        )
