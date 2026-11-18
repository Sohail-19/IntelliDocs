"""Deterministic, gold-labeled answer metrics independent of verifier decisions."""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.answering import AnswerDraft
from app.chunking import canonical_document
from app.verification.matching import match_quote
from intellidocs_intelligence.models import Citation, ParsedDocument

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class AnswerPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: NonBlank
    aliases: list[NonBlank] = Field(default_factory=list)


class RelevantPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    doc_id: NonBlank
    page: int = Field(ge=1)


class GoldAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: NonBlank
    query: NonBlank
    language: Annotated[str, Field(min_length=2)] | None = None
    expected_answer_points: list[NonBlank | AnswerPoint]
    relevant: list[RelevantPage]
    answerable: bool
    filters: dict[str, str] | None = None

    @model_validator(mode="after")
    def consistent_labels(self) -> Self:
        if self.answerable and (not self.expected_answer_points or not self.relevant):
            raise ValueError("Answerable queries require answer points and relevant document/pages")
        if not self.answerable and (self.expected_answer_points or self.relevant):
            raise ValueError("Unanswerable queries require empty answer points and relevant pages")
        variants = [
            normalized(text) for point in self.expected_answer_points for text in texts(point)
        ]
        if len(set(variants)) != len(variants):
            raise ValueError("Answer point text and aliases must be distinct")
        return self


@dataclass(frozen=True)
class ScoredClaim:
    text: str
    citations: Sequence[Citation | None]


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def texts(point: str | AnswerPoint) -> list[str]:
    return [point] if isinstance(point, str) else [point.text, *point.aliases]


def baseline_claims(draft: AnswerDraft) -> list[ScoredClaim]:
    """Retain every raw claim; unresolved source references count as failed citations."""
    output = []
    for claim in draft.claims:
        matches = [
            chunk for chunk in draft.chunks if (chunk.chunk_id or chunk.id) == claim.chunk_id
        ]
        citation = None
        if len(matches) == 1:
            chunk = matches[0]
            span = match_quote(chunk.text, claim.quote, 1.0)
            if span is not None:
                first = chunk.page_start or chunk.page
                citation = Citation(
                    doc_id=chunk.doc_id,
                    page=first,
                    page_start=first,
                    page_end=chunk.page_end or first,
                    char_start=chunk.char_start + span[0],
                    char_end=chunk.char_start + span[1],
                    quote=chunk.text[span[0] : span[1]],
                )
        output.append(ScoredClaim(claim.text, [citation]))
    return output


def grounded(citation: Citation, draft: AnswerDraft) -> bool:
    first, last = citation.page_start or citation.page, citation.page_end or citation.page
    if citation.page != first or first > last or citation.char_start >= citation.char_end:
        return False
    sources = [
        chunk
        for chunk in draft.chunks
        if chunk.doc_id == citation.doc_id
        and chunk.char_start <= citation.char_start < citation.char_end <= chunk.char_end
        and chunk.text[
            citation.char_start - chunk.char_start : citation.char_end - chunk.char_start
        ]
        == citation.quote
    ]
    if not sources:
        return False
    if draft.documents is None:
        return any(
            first == last == (chunk.page_start or chunk.page) == (chunk.page_end or chunk.page)
            for chunk in sources
        )
    document: ParsedDocument | None = draft.documents.get(citation.doc_id)
    if document is None:
        return False
    canonical = canonical_document(document)
    spans = [
        span
        for span in canonical.blocks
        if span.start < citation.char_end and span.end > citation.char_start
    ]
    return (
        canonical.text[citation.char_start : citation.char_end] == citation.quote
        and bool(spans)
        and not any(span.block.type == "header_footer" for span in spans)
        and (first, last) == (spans[0].page, spans[-1].page)
    )


def score(gold: GoldAnswer, claims: Sequence[ScoredClaim], draft: AnswerDraft) -> dict[str, int]:
    covered: set[int] = set()
    correct = attempted = unsupported = 0
    for claim in claims:
        points = {
            index
            for index, point in enumerate(gold.expected_answer_points)
            if normalized(claim.text) in {normalized(text) for text in texts(point)}
        }
        supported = False
        for citation in claim.citations:
            attempted += 1
            valid = (
                bool(points)
                and citation is not None
                and grounded(citation, draft)
                and any(
                    target.doc_id == citation.doc_id
                    and (citation.page_start or citation.page)
                    <= target.page
                    <= (citation.page_end or citation.page)
                    for target in gold.relevant
                )
            )
            correct += int(valid)
            supported |= valid
        if supported:
            covered.update(points)
        else:
            unsupported += 1
    return {
        "queries": 1,
        "claims": len(claims),
        "unsupported_claims": unsupported,
        "citations": attempted,
        "correct_citations": correct,
        "answerable_questions": int(gold.answerable),
        "unanswerable_questions": int(not gold.answerable),
        "abstained_unanswerable": int(not gold.answerable and not claims),
        "expected_points": len(gold.expected_answer_points),
        "covered_points": len(covered),
    }


def summarize(rows: Sequence[Mapping[str, int]]) -> dict[str, int | float | None]:
    counts = {key: sum(row[key] for row in rows) for key in rows[0]} if rows else {}

    def ratio(numerator: str, denominator: str) -> float | None:
        return counts[numerator] / counts[denominator] if counts.get(denominator) else None

    return {
        **counts,
        "citation_precision": ratio("correct_citations", "citations"),
        "unsupported_claim_rate": ratio("unsupported_claims", "claims"),
        "abstention_rate_unanswerable": ratio("abstained_unanswerable", "unanswerable_questions"),
        "answer_coverage": ratio("covered_points", "expected_points"),
    }
