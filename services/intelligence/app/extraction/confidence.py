"""Deterministic weights for evidence found in verified obligation sources."""

import os
from collections.abc import Mapping
from math import fsum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from intellidocs_intelligence.models import ConfidenceBreakdown, ConfidenceContribution


class ConfidenceWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_exact: ConfidenceContribution = 0.25
    quote_fuzzy: ConfidenceContribution = 0.12
    owner_resolved: ConfidenceContribution = 0.55
    deadline_resolved: ConfidenceContribution = 0.10
    recognized_cue: ConfidenceContribution = 0.05
    cross_chunk_agreement: ConfidenceContribution = 0.05

    @model_validator(mode="after")
    def prioritize_exact_match(self) -> Self:
        if self.quote_exact <= self.quote_fuzzy:
            raise ValueError("quote_exact must have a higher weight than quote_fuzzy")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        values = os.environ if env is None else env
        overrides = values.get("EXTRACTION_CONFIDENCE_WEIGHTS")
        return cls.model_validate_json(overrides) if overrides else cls()

    def contributions(
        self,
        *,
        quote_exact: bool,
        quote_fuzzy: bool,
        owner_resolved: bool,
        deadline_resolved: bool,
        recognized_cue: bool,
        cross_chunk_agreement: bool,
    ) -> ConfidenceBreakdown:
        maximum = fsum(
            (
                self.quote_exact,
                self.owner_resolved,
                self.deadline_resolved,
                self.recognized_cue,
                self.cross_chunk_agreement,
            )
        )
        return ConfidenceBreakdown(
            quote_exact=self.quote_exact / maximum if quote_exact else 0.0,
            quote_fuzzy=self.quote_fuzzy / maximum if quote_fuzzy else 0.0,
            owner_resolved=self.owner_resolved / maximum if owner_resolved else 0.0,
            deadline_resolved=self.deadline_resolved / maximum if deadline_resolved else 0.0,
            recognized_cue=self.recognized_cue / maximum if recognized_cue else 0.0,
            cross_chunk_agreement=self.cross_chunk_agreement / maximum
            if cross_chunk_agreement
            else 0.0,
        )
