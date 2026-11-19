"""Pydantic models mirroring the versioned schemas in /contracts."""

from datetime import date, datetime
from math import fsum, isclose
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    SerializerFunctionWrapHandler,
    StrictBool,
    field_validator,
    model_serializer,
    model_validator,
)

NonEmptyString = Annotated[str, Field(min_length=1)]
LanguageCode = Annotated[str, Field(min_length=2)]
ObligationType = Literal["duty", "prohibition", "reporting", "payment", "review", "other"]


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
    org_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
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
    org_id: NonEmptyString | None = None


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
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    clause_number: NonEmptyString | None = None
    heading_path: list[NonEmptyString] | None = None


class ObligationOwner(ContractModel):
    raw_text: str
    resolved_department: NonEmptyString | None
    resolved_role: NonEmptyString | None
    resolved: StrictBool


class ObligationDeadline(ContractModel):
    raw_text: str
    normalized_date: date | None
    is_relative: StrictBool
    anchor_date: date | None
    resolved: StrictBool

    @field_validator("normalized_date", "anchor_date", mode="before")
    @classmethod
    def calendar_date(cls, value: object) -> object:
        return UploadMetadata.explicit_date(value)


ConfidenceContribution = Annotated[FiniteFloat, Field(ge=0, le=1, strict=True)]


class ConfidenceBreakdown(ContractModel):
    quote_exact: ConfidenceContribution
    quote_fuzzy: ConfidenceContribution
    owner_resolved: ConfidenceContribution
    deadline_resolved: ConfidenceContribution
    recognized_cue: ConfidenceContribution
    cross_chunk_agreement: ConfidenceContribution

    @property
    def total(self) -> float:
        return fsum(
            (
                self.quote_exact,
                self.quote_fuzzy,
                self.owner_resolved,
                self.deadline_resolved,
                self.recognized_cue,
                self.cross_chunk_agreement,
            )
        )

    @model_validator(mode="after")
    def one_quote_match(self) -> Self:
        if self.quote_exact and self.quote_fuzzy:
            raise ValueError("Quote evidence cannot be both exact and fuzzy")
        return self


class Obligation(ContractModel):
    id: NonEmptyString
    action: NonEmptyString
    owner: ObligationOwner | NonEmptyString
    deadline: ObligationDeadline | datetime
    source: Citation | None = None
    confidence: FiniteFloat = Field(ge=0, le=1)
    confidence_breakdown: ConfidenceBreakdown | None = None
    status: Literal[
        "extracted",
        "verified",
        "needs_review",
        "rejected",
        "open",
        "in_progress",
        "complete",
        "cancelled",
    ]
    doc_id: NonEmptyString | None = None
    condition: NonEmptyString | None = None
    obligation_type: ObligationType | None = None
    citations: list[Citation] | None = None
    extractor_version: NonEmptyString | None = None

    @model_validator(mode="after")
    def compatible_shape(self) -> Self:
        if self.confidence_breakdown is not None and not isclose(
            self.confidence, self.confidence_breakdown.total, rel_tol=0, abs_tol=1e-12
        ):
            raise ValueError("Confidence must equal the sum of its evidence contributions")
        if self.status in {"open", "in_progress", "complete", "cancelled"}:
            if (
                not isinstance(self.owner, str)
                or not isinstance(self.deadline, datetime)
                or self.source is None
            ):
                raise ValueError(
                    "Legacy obligations require a string owner, timestamp deadline and source"
                )
        else:
            if not isinstance(self.owner, ObligationOwner) or not isinstance(
                self.deadline, ObligationDeadline
            ):
                raise ValueError("Extraction obligations require structured owner and deadline")
            for name in ("doc_id", "obligation_type", "citations", "extractor_version"):
                if getattr(self, name) is None:
                    raise ValueError(f"Extraction obligations require {name}")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_legacy_and_optional_fields(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        # Only omit this object's absent fields. Nested resolution objects must
        # retain required null values for unknown departments, roles and dates.
        value: dict[str, object] = handler(self)
        source = value.get("source")
        if isinstance(source, dict):
            value["source"] = {name: item for name, item in source.items() if item is not None}
        citations = value.get("citations")
        if isinstance(citations, list):
            value["citations"] = [
                {name: item for name, item in citation.items() if item is not None}
                if isinstance(citation, dict)
                else citation
                for citation in citations
            ]
        return {name: item for name, item in value.items() if item is not None}


class ExtractionStats(ContractModel):
    total: int = Field(ge=0, strict=True)
    extracted: int = Field(ge=0, strict=True)
    verified: int = Field(ge=0, strict=True)
    needs_review: int = Field(ge=0, strict=True)
    rejected: int = Field(ge=0, strict=True)


class ExtractionResult(ContractModel):
    doc_id: NonEmptyString
    obligations: list[Obligation]
    warnings: list[str]
    stats: ExtractionStats


class SearchResult(ContractModel):
    chunk: Chunk
    score: FiniteFloat
    rerank_score: FiniteFloat | None = None
    source: Literal["dense", "sparse", "fused"] | None = None
    citations: list[Citation] = Field(min_length=1)


class LegacyRoutingRule(ContractModel):
    document_type: NonEmptyString
    department: NonEmptyString


RoutingText = Annotated[str, Field(pattern=r"\S")]


class RoutingConditions(ContractModel):
    obligation_type: ObligationType | None = None
    owner_department: RoutingText | None = None
    document_type: RoutingText | None = None
    keywords: list[RoutingText] | None = Field(default=None, min_length=1)
    deadline_within_days: int | None = Field(default=None, ge=0, le=36500, strict=True)


class RoutingTarget(ContractModel):
    kind: Literal["user", "role_queue", "department_queue"]
    value: RoutingText


class RoutingRule(ContractModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    priority: int = Field(ge=0, le=2147483647, strict=True)
    conditions: RoutingConditions
    target: RoutingTarget
    escalation_after_days: int | None = Field(default=None, ge=0, le=36500, strict=True)


class RoutingPolicy(ContractModel):
    min_confidence: FiniteFloat = Field(ge=0, le=1)
    review_role: RoutingText


class ObligationDetectionRules(ContractModel):
    cues: list[Annotated[str, Field(pattern=r"\S")]] = Field(default_factory=list)
    date_patterns: list[Annotated[str, Field(pattern=r"\S")]] = Field(default_factory=list)
    relative_deadline_patterns: list[NonEmptyString] = Field(default_factory=list)
    absolute_deadline_wrappers: list[NonEmptyString] = Field(default_factory=list)


class OrgRole(ContractModel):
    department: NonEmptyString | None = None
    aliases: list[NonEmptyString] = Field(default_factory=list)


class OrgAccess(ContractModel):
    bootstrap_role: NonEmptyString
    registration_role: NonEmptyString
    role_permissions: dict[str, list[NonEmptyString]]


class OrgConfig(ContractModel):
    access: OrgAccess | None = None
    upload_defaults: UploadMetadata | None = None
    languages: list[LanguageCode] = Field(min_length=1)
    ocr_engine: NonEmptyString
    document_types: list[NonEmptyString]
    departments: list[NonEmptyString]
    routing_policy: RoutingPolicy | None = None
    routing_rules: list[RoutingRule | LegacyRoutingRule]
    obligation_detection: dict[str, ObligationDetectionRules] = Field(default_factory=dict)
    department_aliases: dict[str, list[NonEmptyString]] = Field(default_factory=dict)
    roles: dict[str, OrgRole] = Field(default_factory=dict)


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
    top_k: int = Field(ge=1, le=100, strict=True)
    debug: StrictBool = False

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must contain non-whitespace text")
        return value


class SearchTimings(ContractModel):
    retrieve: FiniteFloat = Field(ge=0)
    rerank: FiniteFloat = Field(ge=0)
    citations: FiniteFloat = Field(ge=0)


class SearchDebug(ContractModel):
    retriever: NonEmptyString
    reranker: NonEmptyString
    timings_ms: SearchTimings


class SearchResponse(ContractModel):
    results: list[SearchResult]
    debug: SearchDebug | None = None


class AnswerRequest(ContractModel):
    query: NonEmptyString = Field(pattern=r"\S")
    filters: dict[str, str] | None = None
    top_k: int = Field(ge=1, le=100, strict=True)
    language: LanguageCode


class Claim(ContractModel):
    text: NonEmptyString
    citations: list[Citation]
    status: Literal["verified", "unverified"]


class Answer(ContractModel):
    answer_text: str
    claims: list[Claim]
    abstained: StrictBool
    abstain_reason: str
    retrieved_chunk_ids: list[NonEmptyString]
    model: NonEmptyString
    verifier_version: NonEmptyString


class ExtractObligationsRequest(ContractModel):
    doc_id: NonEmptyString


class ExtractObligationsResponse(ContractModel):
    obligations: list[Obligation]
    doc_id: NonEmptyString | None = None
    warnings: list[str] | None = None
    stats: ExtractionStats | None = None
