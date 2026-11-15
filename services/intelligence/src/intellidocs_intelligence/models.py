"""Pydantic models mirroring the versioned schemas in /contracts."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NonEmptyString = Annotated[str, Field(min_length=1)]
LanguageCode = Annotated[str, Field(min_length=2)]


class ContractModel(BaseModel):
    """Base model that rejects fields not present in a contract."""

    model_config = ConfigDict(extra="forbid")


class Block(ContractModel):
    type: Literal["paragraph", "heading", "table", "list_item", "clause", "header_footer"]
    text: str
    bbox: list[Annotated[float, Field(ge=0)]] = Field(min_length=4, max_length=4)
    reading_order: int = Field(ge=0)
    clause_number: NonEmptyString | None = None
    heading_level: int | None = Field(default=None, ge=1)
    heading_path: list[NonEmptyString] | None = None
    rows: list[list[str]] | None = None


class TableBlock(Block):
    type: Literal["table"] = "table"
    rows: list[list[str]]


class TextBlock(Block):
    type: Literal["paragraph", "heading", "list_item", "clause", "header_footer"]


class Page(ContractModel):
    page: int = Field(ge=1)
    language: LanguageCode
    blocks: list[Annotated[TableBlock | TextBlock, Field(discriminator="type")]]


class UploadMetadata(ContractModel):
    department: NonEmptyString | None = None
    doc_type: NonEmptyString | None = None
    doc_date: date | None = None

    @field_validator("doc_date", mode="before")
    @classmethod
    def explicit_date(cls, value: object) -> object:
        if value is None or type(value) is date:
            return value
        if isinstance(value, str):
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("doc_date must be an explicit YYYY-MM-DD date")


class Document(UploadMetadata):
    id: NonEmptyString
    filename: NonEmptyString
    mime_type: NonEmptyString
    source_uri: NonEmptyString
    parent_document_id: NonEmptyString | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    language: LanguageCode | None = None
    status: Literal["pending", "processing", "ready", "failed"]
    pages: list[Page] = Field(default_factory=list)


class ParsedDocument(Document):
    pages: list[Page]


class ParseResult(ContractModel):
    document: ParsedDocument
    parser_name: NonEmptyString
    parser_version: NonEmptyString
    warnings: list[str]
    attachments: list["ParseResult"] | None = None


class Chunk(ContractModel):
    id: NonEmptyString
    doc_id: NonEmptyString
    page: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text: NonEmptyString
    chunk_id: NonEmptyString | None = None
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    block_ids: list[NonEmptyString] | None = None
    clause_number: NonEmptyString | None = None
    heading_path: list[NonEmptyString] | None = None
    language: LanguageCode | None = None
    department: NonEmptyString | None = None
    doc_date: date | None = None
    doc_type: NonEmptyString | None = None


class IndexRequest(ContractModel):
    doc_id: NonEmptyString


class IndexResult(ContractModel):
    doc_id: NonEmptyString
    chunk_count: int = Field(ge=0)
    embedding_model: NonEmptyString
    warnings: list[str]


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
    upload_defaults: UploadMetadata | None = None
    languages: list[LanguageCode] = Field(min_length=1)
    ocr_engine: NonEmptyString
    document_types: list[NonEmptyString]
    departments: list[NonEmptyString]
    routing_rules: list[RoutingRule]


class IngestRequest(UploadMetadata):
    file: bytes
    org_id: NonEmptyString | None = None
    index: bool = False


class IngestResponse(ContractModel):
    document_id: NonEmptyString
    parse_result: ParseResult
    index_results: list[IndexResult] | None = None
    index_error: str | None = None


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
