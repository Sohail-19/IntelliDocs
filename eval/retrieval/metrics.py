"""Gold labels and passage-level retrieval metrics."""

from collections.abc import Sequence
from math import log2
from typing import Annotated, Literal, Self, TypedDict

from intellidocs_intelligence.models import Chunk
from pydantic import BaseModel, ConfigDict, Field, model_validator

NonBlank = Annotated[str, Field(min_length=1)]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, frozen=True)

    doc_id: NonBlank
    page: int | None = Field(default=None, ge=1)
    clause_number: NonBlank | None = None

    @model_validator(mode="after")
    def require_span(self) -> Self:
        if self.page is None and self.clause_number is None:
            raise ValueError("Each target needs page, clause_number, or both")
        return self

    def matches(self, chunk: Chunk) -> bool:
        first = chunk.page_start if chunk.page_start is not None else chunk.page
        last = chunk.page_end if chunk.page_end is not None else first
        return (
            self.doc_id == chunk.doc_id
            and (self.page is None or first <= self.page <= last)
            and (self.clause_number is None or self.clause_number == chunk.clause_number)
        )


class GoldQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    id: NonBlank
    query: NonBlank
    language: NonBlank
    query_type: Literal["same_language", "cross_lingual"]
    targets: list[Target] = Field(min_length=1)
    filters: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_targets(self) -> Self:
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("Duplicate relevance targets in query")
        return self


class Metrics(TypedDict):
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float


def score_ranking(ranking: Sequence[Chunk], targets: Sequence[Target]) -> Metrics:
    """Count unique gold targets and give at most one binary gain at each rank."""
    seen: set[Target] = set()
    recalled_at_5 = recalled_at_10 = 0
    reciprocal_rank = dcg = 0.0
    for rank, chunk in enumerate(ranking, start=1):
        new_targets = {target for target in targets if target not in seen and target.matches(chunk)}
        if not new_targets:
            continue
        seen.update(new_targets)
        if not reciprocal_rank:
            reciprocal_rank = 1 / rank
        if rank <= 5:
            recalled_at_5 = len(seen)
        if rank <= 10:
            recalled_at_10 = len(seen)
            dcg += 1 / log2(rank + 1)
    ideal = sum(1 / log2(rank + 1) for rank in range(1, min(len(targets), 10) + 1))
    return {
        "recall_at_5": recalled_at_5 / len(targets),
        "recall_at_10": recalled_at_10 / len(targets),
        "mrr": reciprocal_rank,
        "ndcg_at_10": dcg / ideal,
    }
