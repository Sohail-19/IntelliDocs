"""Pydantic models mirroring the versioned schemas in /contracts."""

import re
from datetime import date, datetime
from math import fsum, isclose
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
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
NonBlankString = Annotated[str, Field(pattern=r"\S")]
LanguageCode = Annotated[str, Field(min_length=2)]
ObligationType = Literal["duty", "prohibition", "reporting", "payment", "review", "other"]
ChangeRelationType = Literal["supersedes", "amends", "withdraws"]
DocumentRelationType = Literal["supersedes", "amends", "withdraws", "conflicts_with", "references"]
RelationScope = Literal["whole_document", "clauses"]
ConfidenceContribution = Annotated[FiniteFloat, Field(ge=0, le=1, strict=True)]


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
    predicted_type: NonEmptyString | None = None
    prediction_confidence: ConfidenceContribution | None = None

    @model_validator(mode="after")
    def prediction_pair(self) -> Self:
        if (self.predicted_type is None) != (self.prediction_confidence is None):
            raise ValueError("predicted_type and prediction_confidence must appear together")
        return self


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
    org_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")


class IndexResult(ContractModel):
    doc_id: NonEmptyString
    chunk_count: int = Field(ge=0)
    embedding_model: NonEmptyString
    warnings: list[str]


class CitationBox(ContractModel):
    page: int = Field(ge=1)
    bbox: list[Annotated[FiniteFloat, Field(ge=0)]] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def has_positive_area(self) -> Self:
        x0, y0, x1, y1 = self.bbox
        if x0 >= x1 or y0 >= y1:
            raise ValueError("Citation box must have positive area")
        return self


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
    boxes: list[CitationBox] | None = None


class DetectRelationsRequest(ContractModel):
    doc_id: NonEmptyString
    org_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")


class RelationConfidenceBreakdown(ContractModel):
    explicit_reference: ConfidenceContribution
    both_quotes_verified: ConfidenceContribution
    date_order_consistent: ConfidenceContribution
    recognized_cue: ConfidenceContribution

    @property
    def total(self) -> float:
        return fsum((
            self.explicit_reference, self.both_quotes_verified,
            self.date_order_consistent, self.recognized_cue,
        ))


class DocumentRelation(ContractModel):
    id: NonEmptyString
    source_doc_id: NonEmptyString
    target_doc_id: NonEmptyString
    relation_type: DocumentRelationType
    scope: RelationScope
    source_citations: list[Citation]
    target_citations: list[Citation]
    confidence: FiniteFloat = Field(ge=0, le=1, strict=True)
    confidence_breakdown: RelationConfidenceBreakdown | None = None
    status: Literal["proposed", "confirmed", "dismissed"]
    needs_review: StrictBool | None = None
    detected_by: NonEmptyString
    created_at: AwareDatetime

    @field_validator("created_at", mode="before")
    @classmethod
    def explicit_timestamp(cls, value: object) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
            value,
        ):
            return datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        raise ValueError("created_at must be an RFC 3339 timestamp with an explicit offset")

    @model_validator(mode="after")
    def check_evidence(self) -> Self:
        if self.status == "confirmed" and (
            self.needs_review or not self.source_citations or not self.target_citations
        ):
            raise ValueError(
                "Confirmed relations require citations on both sides and no pending review"
            )
        if any(citation.doc_id != self.source_doc_id for citation in self.source_citations):
            raise ValueError("Source citations must belong to the source document")
        if any(citation.doc_id != self.target_doc_id for citation in self.target_citations):
            raise ValueError("Target citations must belong to the target document")
        if self.confidence_breakdown is not None and not isclose(
            self.confidence, self.confidence_breakdown.total, abs_tol=1e-9
        ):
            raise ValueError("Relation confidence must equal its evidence contributions")
        return self


class RelationResult(ContractModel):
    doc_id: NonEmptyString
    relations: list[DocumentRelation]
    warnings: list[str]


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


class RelationDetectionRules(ContractModel):
    reference_cues: dict[str, list[RoutingText]] = Field(default_factory=dict)
    change_cues: dict[str, dict[ChangeRelationType, list[RoutingText]]] = Field(
        default_factory=dict
    )
    nonoperative_cues: dict[str, list[RoutingText]] = Field(default_factory=dict)
    related_document_types: dict[str, list[NonEmptyString]] = Field(default_factory=dict)


class OrgRole(ContractModel):
    department: NonEmptyString | None = None
    aliases: list[NonEmptyString] = Field(default_factory=list)


class OrgAccess(ContractModel):
    bootstrap_role: NonEmptyString
    registration_role: NonEmptyString
    role_permissions: dict[str, list[NonEmptyString]]


class SummaryProfile(ContractModel):
    focus_topics: list[NonBlankString]
    responsibilities: list[NonBlankString]
    glossary: dict[str, dict[str, list[NonBlankString]]]


class SummarySettings(ContractModel):
    max_length: int = Field(ge=1, strict=True)
    style: Literal["bullets", "short_paragraph"]


class SummaryConfig(ContractModel):
    settings: SummarySettings
    department_profiles: dict[str, SummaryProfile] = Field(default_factory=dict)
    role_profiles: dict[str, SummaryProfile] = Field(default_factory=dict)
    not_relevant_reasons: dict[str, NonBlankString] = Field(default_factory=dict)


class DocumentStructure(ContractModel):
    clause_labels: list[NonBlankString] = Field(default_factory=list)
    heading_labels: list[NonBlankString] = Field(default_factory=list)
    sentence_abbreviations: list[NonBlankString] = Field(default_factory=list)
    page_number_patterns: list[NonBlankString] = Field(default_factory=list)


class ClassificationConfig(ContractModel):
    backend: Literal["disabled", "tfidf", "llm"]
    model_path: NonBlankString | None = None
    llm_fallback: StrictBool = False
    min_confidence: ConfidenceContribution = 0.55

    @model_validator(mode="after")
    def require_model_path(self) -> Self:
        if self.backend == "tfidf" and self.model_path is None:
            raise ValueError("classification.model_path is required with the tfidf backend")
        return self


class OrgConfig(ContractModel):
    access: OrgAccess | None = None
    upload_defaults: UploadMetadata | None = None
    classification: ClassificationConfig | None = None
    languages: list[LanguageCode] = Field(min_length=1)
    ocr_engine: NonEmptyString
    document_structure: DocumentStructure = Field(default_factory=DocumentStructure)
    document_types: list[NonEmptyString]
    departments: list[NonEmptyString]
    routing_policy: RoutingPolicy | None = None
    routing_rules: list[RoutingRule | LegacyRoutingRule]
    obligation_detection: dict[str, ObligationDetectionRules] = Field(default_factory=dict)
    relation_detection: RelationDetectionRules = Field(default_factory=RelationDetectionRules)
    department_aliases: dict[str, list[NonEmptyString]] = Field(default_factory=dict)
    roles: dict[str, OrgRole] = Field(default_factory=dict)
    summary: SummaryConfig | None = None

    @field_validator("summary")
    @classmethod
    def summary_when_configured(cls, value: SummaryConfig | None) -> SummaryConfig:
        if value is None:
            raise ValueError("omit summary when it is not configured")
        return value


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


class SummaryAudience(ContractModel):
    department: NonBlankString | None = None
    role: NonBlankString | None = None

    @model_validator(mode="after")
    def require_audience(self) -> Self:
        if self.department is None and self.role is None:
            raise ValueError("a summary audience requires a department or role")
        return self


class SummaryRequest(SummaryAudience):
    doc_id: NonBlankString
    language: LanguageCode = Field(pattern=r"\S")
    org_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    assigned_obligations: list[Obligation] = Field(default_factory=list)


class SummaryPoint(ContractModel):
    text: NonBlankString
    citations: list[Citation] = Field(min_length=1)
    obligation_id: NonEmptyString | None = None
    due_date: date | None = None


class Summary(ContractModel):
    doc_id: NonBlankString
    audience: SummaryAudience
    language: LanguageCode = Field(pattern=r"\S")
    points: list[SummaryPoint]
    not_relevant: StrictBool
    relevance_reason: str
    generated_by: NonBlankString
    verifier_version: NonBlankString

    @model_validator(mode="after")
    def check_relevance(self) -> Self:
        if self.not_relevant and (self.points or not self.relevance_reason.strip()):
            raise ValueError("a non-relevant summary requires a reason and no points")
        return self


class ExtractObligationsRequest(ContractModel):
    doc_id: NonEmptyString
    org_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")


class ExtractObligationsResponse(ContractModel):
    obligations: list[Obligation]
    doc_id: NonEmptyString | None = None
    warnings: list[str] | None = None
    stats: ExtractionStats | None = None
