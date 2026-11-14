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
uv run mypy src
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
