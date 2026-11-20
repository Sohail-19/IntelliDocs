# Contracts

Shared, versioned schemas that define the only interface between services.

Document v1 retains its metadata fields and adds optional `pages` so legacy
metadata-only documents remain valid. ParseResult requires pages (which may be
empty for a pending document), parser identity/version, and warnings. Page numbers
are one-based; reading order is zero-based within a page. Bounding boxes contain
exactly four nonnegative coordinates in page-local points, with the origin at the
top left. Table rows are a two-dimensional array of text cells.

Blocks may carry `heading_level` (one-based) and `heading_path` (ancestor heading
texts, outermost first, excluding the current heading itself). Clause prefixes
are retained verbatim as `clause_number`. Parsers keep repeated margin furniture
and page numbers as `header_footer`; body-text consumers must exclude that type.
Table blocks use `rows` as their authoritative content and emit empty `text` to
avoid duplicating flattened table contents.

Ingest now accepts a `multipart/form-data` upload with a required `file` part
and optional `org_id`, `index`, `department`, `doc_type`, and `doc_date` fields.
`index` defaults to false. The response retains `document_id` and adds required
`parse_result`. The previous JSON `file_reference` ingest request is superseded.
The JSON request example represents the multipart field values; it is not a JSON
HTTP request body. Ingest persists originals and their ParseResults locally.
Email ParseResults may include recursive `attachments`; each attachment Document
carries its parent's external `parent_document_id`. Unsupported attachment types
are reported in warnings. DOCX/email coordinates describe logical pages, not
rendered physical layout.

Chunk v1 keeps required legacy `id` and `page` fields. Phase 2 fields are additive
and optional so existing chunk/search payloads remain valid: `chunk_id`,
`page_start`, `page_end`, `block_ids`, `clause_number`, `heading_path`, `language`,
`department`, `doc_date` (YYYY-MM-DD), and `doc_type`. New producers should emit
the enriched fields with `id == chunk_id` and `page == page_start`; page numbers
are one-based and character offsets are zero-based, end-exclusive. Metadata
Department, document type, and document date come only from explicit upload
fields or optional `OrgConfig.upload_defaults`; upload fields take precedence.
Missing metadata stays absent. `doc_date` must be a calendar date in YYYY-MM-DD
format. Document also carries the original file's SHA-256 `content_hash`.

`POST /v1/index` takes IndexRequest (`doc_id`) and returns IndexResult (`doc_id`,
`chunk_count`, `embedding_model`, `warnings`). It loads a stored ParseResult,
chunks, embeds in batches, and persists vectors and payloads. Repeat indexing
reuses cached embeddings when content, chunking settings, and model identity
match, while refreshing metadata. Ingest with `index=true` indexes the message
and its attachments and returns `index_results`. If optional indexing fails,
ingest returns 503 with the stored `document_id`, `parse_result`, any completed
`index_results`, and `index_error`; callers can retry each ID through `/v1/index`.

`POST /v1/search` accepts a nonblank `query`, `top_k` from 1 to 100, and an
optional string map of `filters`. It returns the top results after reranking
the configured retrieval pool. Each result includes the chunk, the original
retrieval score, the retrieval source, and a citation. A scoring reranker also
supplies `rerank_score`. Dense retrieval scores may be negative.

Citations copy the chunk text as `quote` and use zero-based, end-exclusive
Unicode code-point offsets in canonical document text. Page ranges use one-based
inclusive bounds. Search fills both bounds from `page` for legacy single-page
chunks. The clause number is included when available, and an empty heading path
represents a chunk with no heading context.

Set `debug: true` to receive the retriever and reranker names and elapsed
milliseconds for retrieval, reranking, and citation construction.

Indexed Chunk v1 payloads may include `org_id` from the stored ParseResult.
The search and answer filter maps accept `org_id` as an exact match. Core sets
that filter from the authenticated account on every request to either endpoint.
Without this filter, retrieval only considers legacy chunks without an org ID.
Core also sends `org_id` for indexing, extraction, relation detection, and
summaries. Intelligence treats a document in a different organization as missing.

The Phase 4 answer contract defines `POST /v1/answer` with an `AnswerRequest`
body and an `Answer` response. Requests require a nonblank `query`, `top_k`
from 1 to 100, and a requested `language`. Optional `filters` use the search
contract's string-map shape.

An answer includes `answer_text`, `claims`, `abstained`, `abstain_reason`,
`retrieved_chunk_ids`, `model`, and `verifier_version`. Each claim contains
text, citations using the existing Citation schema, and a `verified` or
`unverified` status. Empty `answer_text` and `claims` arrays can represent abstention.
An empty `abstain_reason` represents an answer with no abstention reason.

Phase 5 extends Obligation v1 with two compatible shapes. Legacy payloads retain
string `owner`, timestamp `deadline`, required `source`, and lifecycle status
(`open`, `in_progress`, `complete`, `cancelled`). Their existing example remains
under `extract-obligations-response.v1.json`. New extraction producers use
structured owners/deadlines and require `doc_id`, `obligation_type`, `citations`,
and `extractor_version` alongside `id`, `action`, `confidence`, and `status`.
Mixing legacy owner/deadline representations with an extraction status is invalid.

Structured owners retain `raw_text` and explicitly include nullable
`resolved_department` / `resolved_role` and a `resolved` boolean. Structured
deadlines include `raw_text`, nullable `normalized_date` / `anchor_date`,
`is_relative`, and `resolved`. Dates use calendar `YYYY-MM-DD` strings; unresolved
values are null. Empty raw text represents a missing owner or deadline.
Resolution fields are data only: these contracts do not resolve dates or infer
organization-specific roles/departments. In Python serialization, retain nested
nulls (use normal `model_dump(mode="json")`, without `exclude_none=True`).

`condition` optionally retains a trigger's wording. `obligation_type` is `duty`,
`prohibition`, `reporting`, `payment`, `review`, or `other`. Extraction statuses
are `extracted`, `verified`, `needs_review`, or `rejected`. Confidence remains in
`[0, 1]`; citations reuse Citation v1. The deprecated singular `source` may still
be included for compatibility, but new producers use `citations`.

`POST /v1/extract-obligations` still accepts a document ID and now returns
ExtractionResult: `doc_id`, `obligations`, `warnings`, and `stats`. Stats include
nonnegative integer counters `total`, `extracted`, `verified`, `needs_review`,
and `rejected` for returned obligations. The former response schema still
accepts its legacy envelope and these additive metadata fields.

Extraction uses the stored ParseResult and the document's indexed chunks.
New ParseResults include `org_id` so extraction uses the organization selected
at ingestion. Older ParseResults use `DEFAULT_ORG_ID`. The endpoint returns
404 for a missing document and 409 if its index is missing or incomplete.

Obligation v1 optionally includes `confidence_breakdown`. Its six numeric
fields are the additive contributions from exact or fuzzy quote verification,
owner resolution, deadline resolution, configured cue recognition, and
agreement across chunks. Their sum is `confidence` within floating-point
precision. Exact and fuzzy contributions are mutually exclusive.

OrgConfig v1 accepts structured routing rules alongside the legacy config
format. A structured rule has an `id`, integer `priority`, `conditions`,
and a `target` with a `kind` and `value`. `escalation_after_days` is
optional. Structured rules require `routing_policy`, which supplies
`min_confidence` and `review_role`. Core validates that rule IDs are unique,
the referenced departments, document types, and roles exist, and exactly
one unconditional default runs last. A legacy config without
`routing_policy` leaves automatic routing disabled.

Phase 8 defines `POST /v1/detect-relations`. The request contains `doc_id` and
the response is a RelationResult with the submitted document ID, a list of
relations, and warnings. A DocumentRelation points from the newer document
(`source_doc_id`) to the older one (`target_doc_id`). The scope identifies
either the entire document or specific clauses. Both evidence arrays use
Citation v1. `detected_by` identifies the detector, and `created_at` uses RFC
3339 with an explicit time zone offset.

New detection responses include `needs_review` and `confidence_breakdown`
on each proposal. The four breakdown fields contribute to `confidence`.
A proposal can carry an empty citation list on a side where verification
failed. Confirmed relations require citations on both sides and cannot
carry `needs_review: true`.

`POST /v1/summarize` defines a cited summary of one document. Supply `doc_id`,
`language`, and a canonical `department`, `role`, or both. An unused selector
may be omitted or set to `null`. The response echoes the document, language,
and audience. Every point includes at least one Citation v1 from the document.
Core supplies `org_id` for document ownership checks and `assigned_obligations`
for current, authorized assignments. Stored obligation points carry an
`obligation_id` and a `due_date` when resolved.
When `not_relevant` is true, `points` is empty and `relevance_reason` explains
the decision.

OrgConfig v1 optionally includes `summary`. Its `department_profiles` and
`role_profiles` use canonical names from `departments` and `roles`. Each profile
provides `focus_topics`, `responsibilities`, and a `glossary` that maps configured
language codes to canonical terms and their aliases. `settings.max_length`
counts Unicode code points across point texts. `settings.style` accepts
`bullets` or `short_paragraph`. Set `not_relevant_reasons` by language to
provide a localized explanation when a summary has no points. The example
organization provides English and Hindi glossaries for both departments and
for the finance officer role.
