# Core Service

Node/Express/TypeScript service for platform orchestration and integration with the intelligence service.

`POST /v1/upload` accepts multipart `file` and optional `org_id`, `department`,
`doc_type`, and `doc_date` (YYYY-MM-DD). It forwards them through the OpenAPI-derived
intelligence client, saves document and email attachment rows in one Postgres
transaction with status `parsed`, then calls `/v1/index` for each document.
Attachments link to their parent using `documents.parent_document_id`. Successful
indexing sets status `indexed` and `indexed_at`; failures set status `failed` and
`index_error`. Apply all migrations before uploading:

```sh
npm ci
DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/DB' npm run migrate:up
```

Runtime requires `DATABASE_URL` and `INTELLIGENCE_URL`. `MAX_UPLOAD_BYTES`
defaults to 20 MiB and limits the forwarded file. Intelligence validation errors
are returned without inserting rows. If database persistence fails after a
successful intelligence request, its files remain stored. A successful upload
returns 202 with `index_results`; an indexing failure returns 502 with retained
IDs, the parse result, completed index results, and `index_error`. Other attachments
are still indexed when one fails. Retrying `/v1/index` reuses saved embeddings;
an independent retry does not update the core row automatically. Cross-service
transactions and automatic reconciliation are not implemented.

`GET /api/search` accepts `query` and `top_k`, plus optional filters and
`debug=true`. Use flat filter parameters or `filters[field]` parameters.
Supported filters are `department`, `doc_type` (or `document_type`),
`language`, `doc_id`, and `doc_date` or inclusive `doc_date_from` and
`doc_date_to` in `YYYY-MM-DD` form.

```sh
curl -G http://localhost:3000/api/search \
  --data-urlencode 'query=monthly report deadline' \
  --data-urlencode 'top_k=5' \
  --data-urlencode 'department=legal' \
  --data-urlencode 'debug=true'
```

Core forwards the query to intelligence and validates the v1 response before
returning the `results` envelope. Invalid input returns 422. An unavailable
intelligence service or an invalid upstream response returns 502.

`GET /api/answer` accepts required `query`, `language`, and `top_k` parameters
plus the same filter forms. Use `filters[language]` to filter source language
separately from the requested answer language.

```sh
curl -G http://localhost:3000/api/answer \
  --data-urlencode 'query=monthly report deadline' \
  --data-urlencode 'language=en' \
  --data-urlencode 'top_k=5'
```

The typed client forwards to intelligence's `POST /v1/answer`. Core validates
the Answer contract and rejects unverified claims, missing citations, or answer
text outside the verified claim list. Valid abstentions pass through. Invalid
input returns 422; unavailable intelligence or invalid upstream output returns
502. Verification cannot be disabled through this route.

After `POST /v1/upload` indexes a document or attachment, core runs obligation
extraction and stores its results with an audit entry. Apply migrations before
running this flow. The upload response includes `extraction_results`. If
extraction fails, the upload returns 502 with `extraction_failed_document_ids`
and the successful index results. The document remains indexed and its
extraction status is recorded separately.

- `GET /api/obligations` lists stored obligations and accepts `doc_id`, `status`,
  `owner_department`, `due_before`, and `due_after`. Date bounds are exclusive
  and use YYYY-MM-DD. Obligations without a normalized date do not satisfy
  date filters.
- `GET /api/documents/:id/obligations` uses the external document ID and returns
  obligations with the document's extraction status and any error.
- `POST /api/documents/:id/obligations` reruns extraction for an indexed
  document. Success replaces that document's previous obligations in one
  transaction, including when the result is empty. Failure retains the last
  successful snapshot and records the failed step in the audit log.
