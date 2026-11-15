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
and optional `org_id` field. The response retains `document_id` and adds required
`parse_result`. The previous JSON `file_reference` ingest request is superseded.
The JSON request example represents the multipart field values; it is not a JSON
HTTP request body. Ingest persists originals and their ParseResults locally.
Email ParseResults may include recursive `attachments`; each attachment Document
carries its parent's external `parent_document_id`. Unsupported attachment types
are reported in warnings. DOCX/email coordinates describe logical pages, not
rendered physical layout.
