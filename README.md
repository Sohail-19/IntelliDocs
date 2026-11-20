# IntelliDocs

IntelliDocs is a domain-agnostic document-intelligence platform. The Python
intelligence service owns OCR-adjacent document understanding, retrieval, and
grounded extraction interfaces; the Node core service owns platform
orchestration and persistence. They communicate only through the versioned
schemas and OpenAPI contract in [`contracts/`](contracts/).

## Run with Docker

Copy the environment template and fill in local values, including distinct
`AUTH_JWT_SECRET` and `AUTH_BOOTSTRAP_TOKEN` secrets. Start Postgres and apply
the core migrations before starting the full stack:

```sh
cp .env.example .env
docker compose up -d postgres
npm ci --prefix services/core
DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/DB' npm --prefix services/core run migrate:up
docker compose up --build
```

The services expose health endpoints at:

```sh
curl http://localhost:8000/health
curl http://localhost:3000/health
```

Stop the stack with `docker compose down`.

Open the [web app](http://localhost:5173) to sign in to the workspace. The
web build uses `VITE_API_URL`, and core's `CORE_CORS_ORIGINS` must include
the web origin. See the [web README](apps/web/README.md) for local Vite
development.

Register the first administrator and log in through core to get an access
token. See [core authentication](services/core/README.md#authentication).
Supply the token when uploading:

```sh
curl --fail -H "Authorization: Bearer $ACCESS_TOKEN" -F file=@document.pdf http://localhost:3000/v1/upload
```

Intelligence, Qdrant, and Postgres bind to loopback for local debugging. Core
is the authenticated client-facing service.
`MAX_UPLOAD_BYTES` controls the size limit; `STORAGE_DIR` names its container-local
storage path, backed by the `intelligence_uploads` volume. Organization selection
uses `org_id` or `DEFAULT_ORG_ID`, and reads engines/languages from `ORG_CONFIG_DIR`.

Core uploads also index the document and its attachments, recording `parsed`,
`indexed`, or `failed` on each Postgres document row. Intelligence uploads index
only when `-F index=true` is supplied. Both accept optional `department`,
`doc_type`, and `doc_date` (YYYY-MM-DD); missing values come only from explicit
`upload_defaults` in the org config. Configure `CHUNK_TARGET_TOKENS`,
`CHUNK_OVERLAP_TOKENS`, `CHUNK_TOKEN_COUNT_METHOD`, `EMBEDDING_BATCH_SIZE`, and the
embedding backend in `.env`. Re-indexing an unchanged document reuses persisted
embeddings. The default fake embedder supports local checks; semantic vectors
require the optional BGE-M3 extra and model files provisioned before runtime.

## Core API

Core is the authenticated API for clients. Endpoint behavior and payloads are
covered in the [core service README](services/core/README.md).

| Endpoint | Required permission or credential |
| --- | --- |
| `GET /health` | Public |
| `POST /api/auth/register` | Bootstrap token for the first account, then `users:register` |
| `POST /api/auth/login` | Organization, email, and password |
| `POST /api/auth/refresh` | Current refresh token |
| `GET /api/auth/me` | Valid access token |
| `POST /api/auth/logout` | Current refresh token |
| `GET /api/users` | `users:roles` |
| `PATCH /api/users/:id` | `users:roles` |
| `PUT /api/users/:id/roles` | `users:roles` |
| `GET /api/organisation` | `org:read` |
| `POST /v1/upload` | `documents:upload` |
| `GET /api/documents` | `documents:upload` |
| `GET /api/documents/upload-options` | `documents:upload` |
| `GET /api/documents/:id` | `documents:upload`, `documents:search`, or `documents:answer` |
| `GET /api/documents/:id/summary` | Document access, plus `documents:summary-preview` for audience overrides |
| `POST /api/documents/:id/index` | `documents:upload` |
| `GET /api/search` | `documents:search` |
| `GET /api/search/options` | `documents:search` or `documents:answer` |
| `GET /api/documents/:id/source` | Document access, visible task, or review access |
| `GET /api/answer` | `documents:answer` |
| `GET /api/obligations` | `obligations:read` |
| `GET /api/documents/:id/obligations` | `obligations:read` |
| `POST /api/documents/:id/obligations` | `obligations:extract` |
| `PATCH /api/documents/:id/obligations/:obligation_id/status` | `obligations:status` |
| `PATCH /api/documents/:id/obligations/:obligation_id/assignment` | `obligations:assign` |
| `POST /api/documents/:id/obligations/:obligation_id/route` | `obligations:assign` |
| `GET /api/obligations/review` | `obligations:review` |
| `GET /api/obligations/review/options` | `obligations:review` |
| `PATCH /api/documents/:id/obligations/:obligation_id/review` | `obligations:review` |
| `POST /api/documents/:id/obligations/:obligation_id/review/approve` | `obligations:review` |
| `POST /api/documents/:id/obligations/:obligation_id/review/reject` | `obligations:review` |
| `GET /api/tasks` | `tasks:read` |
| `PATCH /api/tasks/:id/status` | `tasks:transition` and access to the active assignment |
| `GET /api/audit` | `audit:read` |
| `GET /api/relations`, `GET /api/relations/:id` | `relations:review` |
| `POST /api/relations/:id/confirm`, `POST /api/relations/:id/dismiss` | `relations:review` |
| `PATCH /api/relations/:id/type` | `relations:review` |
| `GET /api/relations/:id/stale-obligations`, `PATCH /api/relations/:id/stale-obligations/:obligation_id` | `relations:review` |

## Roles and permissions

Grants come from `access.role_permissions` in each org config. These are the
grants in the bundled example org.

| Permissions | Roles |
| --- | --- |
| `documents:search`, `documents:answer`, `obligations:read`, `tasks:read` | `admin`, `manager`, `officer`, `finance_officer`, `viewer` |
| `documents:summary-preview` | `admin`, `manager` |
| `documents:upload`, `tasks:transition` | `admin`, `manager`, `officer`, `finance_officer` |
| `audit:read`, `org:read`, `obligations:extract`, `obligations:status`, `obligations:assign`, `obligations:review`, `relations:review` | `admin`, `manager` |
| `users:register`, `users:roles` | `admin` |

## Intelligence API

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Check service health |
| `POST /v1/ingest` | Parse an uploaded file and optionally index it |
| `POST /v1/index` | Index an already parsed document |
| `GET /v1/documents/{doc_id}/source` | Return the original PDF for the given organization |
| `POST /v1/search` | Retrieve cited document chunks |
| `POST /v1/answer` | Generate and verify a grounded answer |
| `POST /v1/summarize` | Generate a verified summary for a document audience |
| `POST /v1/extract-obligations` | Extract, verify, and resolve obligations |
| `POST /v1/detect-relations` | Propose relations with evidence from both documents |

The [intelligence OpenAPI contract](contracts/openapi/intelligence.v1.yaml)
defines requests and responses.

## Run the browser demo

Use Node 20.19 or later, Docker with Compose v2.24.4 or later, and OpenSSL.
From the repository root, run:

```sh
npm ci --prefix services/core
npm ci --prefix e2e
(cd e2e && npx playwright install --only-shell chromium)
bash e2e/run-compose.sh
```

On Linux, add `--with-deps` to the Chromium install command if system
dependencies are missing. The script creates its own Compose project and
database, applies migrations, and supplies an in-network mock LLM plus fake
embeddings. It registers a temporary admin, uploads a synthetic PDF, verifies
the highlighted citation in Search, completes the routed task, and checks the
audit trail. It then uploads an original circular and an amendment, confirms
the relation, follows the link to the newer document, and sees the original
task in the stale queue. It removes its containers and volumes on exit. The separate
**Docker demo** workflow runs the same check and saves screenshots on failure.

The demo uses loopback ports 55173 (web), 53000 (core), 58000 (intelligence),
55432 (Postgres), and 56333 (Qdrant). Set the corresponding `DEMO_WEB_PORT`,
`DEMO_CORE_PORT`, `DEMO_INTELLIGENCE_PORT`, `DEMO_POSTGRES_PORT`, or
`DEMO_QDRANT_PORT` if a port is in use.

## Screenshot placeholders

| Screen | Planned screenshot |
| --- | --- |
| Search | Original PDF with a highlighted citation |
| Task feed | A routed obligation and its available status actions |
| Audit trail | Filtered task transition with before and after values |
| Users and roles | Configured department and role controls |
| Organisation | Loaded languages and routing rules |

## Run checks locally

Contracts require only the Python standard library:

```sh
python3 scripts/validate_contracts.py
```

For the intelligence service (Python 3.11 and uv):

```sh
cd services/intelligence
uv sync --all-groups
uv run ruff check .
uv run mypy
uv run pytest
```

For the core service (Node 20):

```sh
cd services/core
npm ci
npm run lint
npm run typecheck
npm test
```
