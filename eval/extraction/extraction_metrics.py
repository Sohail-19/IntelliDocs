"""Gold labels and metrics for obligation extraction and source faithfulness."""

import unicodedata
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import date
from difflib import SequenceMatcher
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.chunking.canonical import CanonicalDocument, canonical_document
from intellidocs_intelligence.models import (
    Citation,
    ExtractionResult,
    Obligation,
    ObligationDeadline,
    ObligationOwner,
    ParsedDocument,
)

NonBlank = Annotated[str, Field(pattern=r"\S")]


class GoldOwner(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    department: NonBlank | None
    role: NonBlank | None


class GoldObligation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    action: NonBlank
    owner: GoldOwner
    deadline: date | None
    page: int = Field(ge=1)
    clause_number: NonBlank | None = None


class GoldDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    doc_id: NonBlank
    language: Annotated[str, Field(pattern=r"^[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*$")]
    obligations: list[GoldObligation]


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text.casefold()).split())


def similarity(left: str, right: str) -> float:
    # SequenceMatcher can break ties differently depending on argument order.
    forward = SequenceMatcher(None, left, right, autojunk=False).ratio()
    reverse = SequenceMatcher(None, right, left, autojunk=False).ratio()
    return (forward + reverse) / 2


def location_matches(prediction: Obligation, label: GoldObligation) -> bool:
    return any(
        citation.doc_id == prediction.doc_id
        and (citation.page_start or citation.page)
        <= label.page
        <= (citation.page_end or citation.page)
        and (label.clause_number is None or citation.clause_number == label.clause_number)
        for citation in prediction.citations or []
    )


def match_actions(
    predictions: Sequence[Obligation],
    labels: Sequence[GoldObligation],
    threshold: float,
) -> list[tuple[int, int]]:
    """Find a deterministic maximum-cardinality one-to-one matching."""
    normalized_labels = [normalize(label.action) for label in labels]
    edges = []
    for item in predictions:
        action = normalize(item.action)
        choices = []
        for index, target in enumerate(normalized_labels):
            score = similarity(action, target)
            if score >= threshold:
                choices.append((-score, not location_matches(item, labels[index]), index))
        edges.append([index for _, _, index in sorted(choices)])

    # Reroute existing pairs along an augmenting path if a new prediction
    # has no free target. A straight greedy assignment can undercount matches.
    by_label: dict[int, int] = {}
    by_prediction: dict[int, int] = {}
    for root in range(len(predictions)):
        pending = deque([root])
        seen_predictions = {root}
        parents: dict[int, int] = {}
        while pending:
            prediction = pending.popleft()
            for label in edges[prediction]:
                if label in parents:
                    continue
                parents[label] = prediction
                previous = by_label.get(label)
                if previous is None:
                    break
                if previous not in seen_predictions:
                    seen_predictions.add(previous)
                    pending.append(previous)
            else:
                continue
            current: int | None = label
            while current is not None:
                prediction = parents[current]
                by_label[current] = prediction
                previous_label = by_prediction.get(prediction)
                by_prediction[prediction] = current
                current = previous_label
            break
    return sorted(by_prediction.items())


def faithful(citation: Citation, doc_id: str, source: CanonicalDocument) -> bool:
    start, end = citation.char_start, citation.char_end
    if citation.doc_id != doc_id or not 0 <= start < end <= len(source.text):
        return False
    if not citation.quote.strip() or citation.quote not in source.text[start:end]:
        return False
    pages = [
        span.page
        for span in source.blocks
        if span.start < span.end and span.start < end and span.end > start
    ]
    first = citation.page_start or citation.page
    last = citation.page_end or citation.page
    return bool(pages) and first == citation.page == pages[0] and last == pages[-1]


@dataclass(frozen=True)
class Counts:
    documents: int
    gold: int
    predictions: int
    matched: int
    rejected: int
    needs_review: int
    owners_correct: int
    deadlines_correct: int
    citations: int
    faithful_citations: int
    uncited: int


def _owner_correct(actual: ObligationOwner, expected: GoldOwner) -> bool:
    values = (actual.resolved_department, actual.resolved_role)
    labels = (expected.department, expected.role)
    return actual.resolved == any(labels) and tuple(
        normalize(value) if value is not None else None for value in values
    ) == tuple(normalize(value) if value is not None else None for value in labels)


def score_document(
    gold: GoldDocument,
    result: ExtractionResult,
    document: ParsedDocument,
    threshold: float,
) -> Counts:
    if gold.doc_id != result.doc_id or gold.doc_id != document.id:
        raise ValueError("Gold, prediction and source IDs must agree")
    source = canonical_document(document)
    if any(label.page not in {page.page for page in document.pages} for label in gold.obligations):
        raise ValueError("A gold obligation cites a page absent from the source document")
    if any(item.doc_id != gold.doc_id for item in result.obligations):
        raise ValueError("Every prediction must belong to the result document")
    if any(not isinstance(item.owner, ObligationOwner) for item in result.obligations):
        raise ValueError("Evaluation requires current structured obligation results")

    predictions = [item for item in result.obligations if item.status != "rejected"]
    matched = match_actions(predictions, gold.obligations, threshold)
    owners_correct = deadlines_correct = 0
    for prediction, label in matched:
        item, target = predictions[prediction], gold.obligations[label]
        assert isinstance(item.owner, ObligationOwner) and isinstance(
            item.deadline, ObligationDeadline
        )
        owners_correct += _owner_correct(item.owner, target.owner)
        deadlines_correct += (
            item.deadline.normalized_date == target.deadline
            and item.deadline.resolved == (target.deadline is not None)
        )
    citations = [citation for item in result.obligations for citation in (item.citations or [])]
    return Counts(
        documents=1,
        gold=len(gold.obligations),
        predictions=len(predictions),
        matched=len(matched),
        rejected=len(result.obligations) - len(predictions),
        needs_review=sum(item.status == "needs_review" for item in result.obligations),
        owners_correct=owners_correct,
        deadlines_correct=deadlines_correct,
        citations=len(citations),
        faithful_citations=sum(faithful(citation, gold.doc_id, source) for citation in citations),
        uncited=sum(not item.citations for item in predictions),
    )


def summarize(rows: Sequence[Counts]) -> dict[str, int | float | None]:
    counts = {field.name: sum(getattr(row, field.name) for row in rows) for field in fields(Counts)}
    matched, predicted, gold = counts["matched"], counts["predictions"], counts["gold"]

    def rate(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    return {
        **counts,
        "false_positives": predicted - matched,
        "false_negatives": gold - matched,
        "precision": rate(matched, predicted),
        "recall": rate(matched, gold),
        "f1": rate(2 * matched, predicted + gold),
        "owner_accuracy": rate(counts["owners_correct"], matched),
        "deadline_accuracy": rate(counts["deadlines_correct"], matched),
        "citation_faithfulness": rate(counts["faithful_citations"], counts["citations"]),
        "needs_review_rate": rate(counts["needs_review"], predicted + counts["rejected"]),
    }
