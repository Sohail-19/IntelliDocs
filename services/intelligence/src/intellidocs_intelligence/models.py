"""Pydantic models mirroring the versioned schemas in /contracts."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

NonEmptyString = Annotated[str, Field(min_length=1)]
LanguageCode = Annotated[str, Field(min_length=2)]


class ContractModel(BaseModel):
    """Base model that rejects fields not present in a contract."""

    model_config = ConfigDict(extra="forbid")


class Document(ContractModel):
    id: NonEmptyString
    filename: NonEmptyString
    mime_type: NonEmptyString
    source_uri: NonEmptyString
    language: LanguageCode | None = None
    status: Literal["pending", "processing", "ready", "failed"]


class Chunk(ContractModel):
    id: NonEmptyString
    doc_id: NonEmptyString
    page: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text: NonEmptyString


class Citation(ContractModel):
    doc_id: NonEmptyString
    page: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    quote: NonEmptyString


class Obligation(ContractModel):
    id: NonEmptyString
    action: NonEmptyString
    owner: NonEmptyString
    deadline: datetime
    source: Citation
    confidence: float = Field(ge=0, le=1)
    status: Literal["open", "in_progress", "complete", "cancelled"]


class SearchResult(ContractModel):
    chunk: Chunk
    score: float = Field(ge=0)
    citations: list[Citation] = Field(min_length=1)


class RoutingRule(ContractModel):
    document_type: NonEmptyString
    department: NonEmptyString


class OrgConfig(ContractModel):
    languages: list[LanguageCode] = Field(min_length=1)
    ocr_engine: NonEmptyString
    document_types: list[NonEmptyString]
    departments: list[NonEmptyString]
    routing_rules: list[RoutingRule]


class IngestRequest(ContractModel):
    file_reference: NonEmptyString


class IngestResponse(ContractModel):
    document_id: NonEmptyString


class SearchRequest(ContractModel):
    query: NonEmptyString
    filters: dict[str, str] | None = None
    top_k: int = Field(ge=1, le=100)


class SearchResponse(ContractModel):
    results: list[SearchResult]


class ExtractObligationsRequest(ContractModel):
    doc_id: NonEmptyString


class ExtractObligationsResponse(ContractModel):
    obligations: list[Obligation]
