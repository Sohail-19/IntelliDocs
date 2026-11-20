"""Select audience evidence and check the citations of stored obligations."""

from collections.abc import Sequence
from datetime import date

from app.extraction.detector import contains_phrase
from app.generation import GeneratedClaim
from app.retrieval import Retriever
from app.verification import CitationVerifier, VerificationConfig
from intellidocs_intelligence.models import (
    Chunk,
    Obligation,
    ObligationDeadline,
    ParsedDocument,
    SummaryPoint,
    SummaryProfile,
)
from intellidocs_intelligence.sparse import tokenize


def _matches(text: str, terms: Sequence[str]) -> bool:
    normalized = " ".join(tokenize(text))
    return any(contains_phrase(normalized, term) for term in terms)


def select_chunks(
    retriever: Retriever,
    doc_id: str,
    org_id: str | None,
    profiles: Sequence[SummaryProfile],
    obligations: Sequence[SummaryPoint],
) -> list[Chunk]:
    vocabulary = []
    for profile in profiles:
        vocabulary.extend(profile.focus_topics)
        vocabulary.extend(profile.responsibilities)
        for glossary in profile.glossary.values():
            for term, aliases in glossary.items():
                vocabulary.append(term)
                vocabulary.extend(aliases)
    terms = list(
        dict.fromkeys(term for phrase in vocabulary if (term := " ".join(tokenize(phrase))))
    )
    if not terms:
        return []
    actions = [
        point.text
        for point in obligations
        if _matches(point.text, terms)
        or any(_matches(citation.quote, terms) for citation in point.citations)
    ]
    query = "\n".join(dict.fromkeys([*vocabulary, *actions]))
    filters = {"doc_id": doc_id}
    if org_id is not None:
        filters["org_id"] = org_id
    chunks: dict[str, Chunk] = {}
    for candidate in retriever.retrieve(query, filters):
        chunk = candidate.chunk
        if chunk.doc_id == doc_id and chunk.org_id == org_id and _matches(chunk.text, terms):
            chunks.setdefault(chunk.chunk_id or chunk.id, chunk)
    return list(chunks.values())


def obligation_points(
    document: ParsedDocument, assigned: Sequence[Obligation]
) -> list[SummaryPoint]:
    """Copy effective stored actions/dates and recheck their source spans."""
    chunks: list[Chunk] = []
    claims: list[GeneratedClaim] = []
    by_chunk: dict[str, str] = {}
    texts: dict[str, str] = {}
    due_dates: dict[str, date | None] = {}
    for obligation in assigned:
        if (
            obligation.doc_id != document.id
            or obligation.status not in {"verified", "needs_review", "extracted"}
            or not obligation.action.strip()
        ):
            continue
        deadline = obligation.deadline
        due_date = (
            deadline.normalized_date
            if isinstance(deadline, ObligationDeadline) and deadline.resolved
            else None
        )
        texts[obligation.id] = (
            obligation.action if due_date is None else f"{obligation.action} — {due_date}"
        )
        due_dates[obligation.id] = due_date
        for citation in obligation.citations or []:
            if citation.doc_id != document.id:
                continue
            identifier = str(len(chunks))
            by_chunk[identifier] = obligation.id
            chunks.append(
                Chunk(
                    id=identifier,
                    doc_id=citation.doc_id,
                    page=citation.page,
                    page_start=citation.page_start,
                    page_end=citation.page_end,
                    text=citation.quote,
                    char_start=citation.char_start,
                    char_end=citation.char_end,
                    clause_number=citation.clause_number,
                    heading_path=citation.heading_path,
                )
            )
            claims.append(
                GeneratedClaim(text=obligation.action, chunk_id=identifier, quote=citation.quote)
            )
    verified = CitationVerifier(VerificationConfig()).verify(
        claims, chunks, documents={document.id: document}
    )
    points: dict[str, SummaryPoint] = {}
    for claim, result in zip(claims, verified, strict=True):
        if result.status != "verified":
            continue
        identifier = by_chunk[claim.chunk_id]
        if identifier not in points:
            points[identifier] = SummaryPoint(
                text=texts[identifier],
                citations=result.citations,
                obligation_id=identifier,
                due_date=due_dates[identifier],
            )
            continue
        citations = points[identifier].citations
        citations.extend(citation for citation in result.citations if citation not in citations)
    return list(points.values())
