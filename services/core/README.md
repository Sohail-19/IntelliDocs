# Core Service

Node/Express/TypeScript service for platform orchestration and integration with the intelligence service.

`POST /v1/upload` accepts multipart `file` and optional `org_id`, forwards them
through the OpenAPI-derived intelligence client, then inserts the returned
document and email attachment rows in one Postgres transaction. Attachments link
to their parent using `documents.parent_document_id`. The response is returned
only after the transaction commits. Apply both migrations before uploading:

```sh
npm ci
DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/DB' npm run migrate:up
```

Runtime requires `DATABASE_URL` and `INTELLIGENCE_URL`. `MAX_UPLOAD_BYTES`
defaults to 20 MiB and limits the forwarded file. Intelligence validation errors
are returned without inserting rows. If database persistence fails after a
successful intelligence request, its files remain stored; cross-service
transactions and reconciliation are not implemented.
