# IntelliDocs web

The web app provides sign-in, a permission-aware workspace, and a document
library with upload, processing status, and obligation details.

## Run locally

Use Node 20.19 or later. Start core with its database and migrations, then run
the web app from this directory.

```sh
npm ci
VITE_API_URL=http://localhost:3000 npm run dev
```

Use a browser-reachable URL for `VITE_API_URL`. Core's
`CORE_CORS_ORIGINS` accepts comma-separated HTTP origins and must include
the web app origin, such as `http://localhost:5173`.

Docker Compose builds the app and serves it at the configured `WEB_PORT`,
which defaults to 5173. The Vite API URL is set at build time, so rebuild the
web image after changing it. Nginx serves the app shell for direct route
links.

## Sessions and permissions

Core sends refresh tokens in JSON. The client holds both tokens in memory
and signs in again after a full page reload. On a 401 it rotates the refresh
token once and retries the original request. Sign-out clears the browser
session immediately and requests refresh token revocation from core.

`GET /api/auth/me` returns the user's effective permissions from org config.
The sidebar and guarded sections use those grants. The shared
[`core-api.v1.d.ts`](../../contracts/core-api.v1.d.ts) contract supplies
the API types to both core and web.

## Documents

Choose files with the picker or drop them onto the upload area. Metadata
applies to the batch and its email attachments. The pickers and file size
limit come from core. Browser checks share the filename extensions and
signatures used by intelligence. Core validates and processes each file as a
separate request.

The library polls while an upload, retry, or stored document is processing.
Opening a document shows its metadata, processing steps, and extracted
obligations for users with `obligations:read`. Retry processing on a saved
failure to reuse its document ID. Files that failed before being saved can
be retried from the upload panel during the current visit.

## Search and Ask

Search returns matching passages with ranking scores. Filters use the
authenticated organization's departments, document types, and languages.
Ask uses the same document filters and a separate answer language. It
displays verified claims and their citations or the reason the answer was
withheld.

Open a citation to view the original PDF on the cited page. The viewer
highlights the blocks intersecting the cited character span and displays
the verified quote. Sources without an original PDF or layout coordinates
still show the page and quote. The PDF.js worker is bundled with the app.

## Tasks and manager review

The task feed shows personal, department, and review queues based on the
scopes core returns. Status buttons come from each task's allowed
transitions. Rejecting requires a reason. Task and review citations use the
same source viewer as Search.

The review screen requires `obligations:review`. It shows confidence
contributions and keeps the extracted owner and deadline visible alongside
current values. Save an override before approving. Approval routes the
obligation immediately. Failed updates restore the prior server state.

## Checks

```sh
npm run lint
npm run typecheck
npm run format:check
npm test
npm run build
```
