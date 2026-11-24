#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export CORE_PORT="${DEMO_CORE_PORT:-53000}"
export WEB_PORT="${DEMO_WEB_PORT:-55173}"
export INTELLIGENCE_PORT="${DEMO_INTELLIGENCE_PORT:-58000}"
export QDRANT_PORT="${DEMO_QDRANT_PORT:-56333}"
export POSTGRES_PORT="${DEMO_POSTGRES_PORT:-55432}"
export POSTGRES_USER=demo POSTGRES_DB=demo
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export AUTH_JWT_SECRET="$(openssl rand -hex 32)"
export AUTH_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"
export INTELLIDOCS_INTERNAL_KEY="$(openssl rand -hex 32)"
export DEMO_PASSWORD="$(openssl rand -hex 24)"
export DEMO_WEB_URL="https://127.0.0.1:$WEB_PORT"
export LLM_PROVIDER=fake LLM_MODEL=fixture LLM_API_KEY='' LLM_BASE_URL=''
export STORAGE_DIR=/data/uploads ORG_CONFIG_DIR=/config/orgs QDRANT_URL=http://qdrant:6333
export SPARSE_EMBEDDING_BACKEND=auto EXTRACTION_CONFIDENCE_WEIGHTS=''
export DATABASE_URL="postgresql://demo:$POSTGRES_PASSWORD@127.0.0.1:$POSTGRES_PORT/demo"

project="intellidocs-demo-$(openssl rand -hex 5)"
E2E_TLS_DIR="$(mktemp -d /tmp/intellidocs-e2e-tls.XXXXXXXX)"
export E2E_TLS_DIR
compose=(docker compose --env-file /dev/null -p "$project"
  -f docker-compose.yml -f e2e/compose.yml -f e2e/browser.compose.yml)
cleanup() {
  local result=$?
  if ((result != 0)); then
    "${compose[@]}" logs --no-color || true
  fi
  "${compose[@]}" down --volumes --remove-orphans || true
  rm -rf -- "$E2E_TLS_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=127.0.0.1' \
  -addext 'subjectAltName=IP:127.0.0.1' \
  -keyout "$E2E_TLS_DIR/key.pem" -out "$E2E_TLS_DIR/cert.pem" 2>/dev/null
# The unprivileged nginx user reads the individually mounted files. The host directory is private.
chmod 0644 "$E2E_TLS_DIR/key.pem" "$E2E_TLS_DIR/cert.pem"

"${compose[@]}" up -d --wait --wait-timeout 120 postgres
npm --prefix services/core run migrate:up
"${compose[@]}" up -d --build --wait --wait-timeout 180
npm --prefix e2e test -- "$@"
