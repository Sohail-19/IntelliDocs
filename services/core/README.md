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
