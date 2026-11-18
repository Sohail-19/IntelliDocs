"""Structured draft claims returned by the generation provider."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

NonEmptyText = Annotated[str, Field(min_length=1)]


class GeneratedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: NonEmptyText
    chunk_id: NonEmptyText
    quote: NonEmptyText


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    claims: list[GeneratedClaim]
