# IntelliDocs

IntelliDocs is a domain-agnostic document-intelligence platform. The Python
intelligence service owns OCR-adjacent document understanding, retrieval, and
grounded extraction interfaces; the Node core service owns platform
orchestration and persistence. They communicate only through the versioned
schemas and OpenAPI contract in [`contracts/`](contracts/).

## Run with Docker

Copy the environment template and replace its placeholders with local values:

```sh
cp .env.example .env
docker compose up --build
```

The services expose health endpoints at:

```sh
curl http://localhost:8000/health
curl http://localhost:3000/health
```

Stop the stack with `docker compose down`.

For uploads, first apply the core migrations using the host Node toolchain (see
[`services/core/README.md`](services/core/README.md)), with `DATABASE_URL` pointing
to the exposed Postgres port. Then upload through core:

```sh
curl --fail -F file=@document.pdf -F org_id=example http://localhost:3000/v1/upload
```

Intelligence also accepts the same fields at `http://localhost:8000/v1/ingest`.
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
