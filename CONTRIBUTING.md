# Contributing

Read [AGENTS.md](AGENTS.md) before editing. Keep changes small, limited to the task, and free of secrets. Put organization-specific languages, departments, document types, and workflow rules in config. Maintain the shared contracts and corresponding types for changes that cross a service boundary.

## Branches and commits

Branch from up-to-date `main`. Use a lowercase, hyphenated description and a prefix such as `feat/`, `fix/`, or `docs/`. For example, `docs/evaluation-guide`. Codex-created branches use `codex/`.

Write [Conventional Commits](https://www.conventionalcommits.org/) messages with `type(scope): imperative summary`. The scope is optional. For example, `docs(eval): explain fixture coverage` or `fix(core): scope task lookup to the caller`. Open a pull request against `main` with the change, the checks you ran, and any remaining verification.

## Local checks

Use Python 3.11 with uv and Node 20.19 or newer. From the repository root, run the checks for the area you changed.

```sh
# Shared contracts
python3 scripts/validate_contracts.py

# Intelligence
(cd services/intelligence && uv sync --all-groups --locked && uv run ruff check . && uv run mypy src && uv run pytest)

# Core and web
(cd services/core && npm ci && npm run lint && npm run typecheck && npm test)
(cd apps/web && npm ci && npm run lint && npm run typecheck && npm test)
```

For changes that span the services and web app, run the Docker Compose Playwright demo. It uses its own local stack, synthetic documents, temporary accounts, and an in-network fixture provider. It checks citations, task completion, the audit trail, and stale tasks after confirming an amendment.

```sh
npm ci --prefix services/core
npm ci --prefix e2e
(cd e2e && npx playwright install --only-shell chromium)
bash e2e/run-compose.sh
```

On Linux, add `--with-deps` to the Chromium install if libraries are missing. Use [the evaluation guide](docs/evaluation.md) for scoring changes and [the onboarding guide](docs/onboarding.md) for org config changes. Keep private input data, generated evaluation reports, local model files, and credentials outside Git.
