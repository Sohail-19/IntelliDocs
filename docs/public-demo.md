# Public demo safeguards

Set `INTELLIDOCS_ENV=production` and `DEMO_MODE=true` on Core and Intelligence.
Apply the Core migrations before starting. Keep `DATABASE_URL`, `QDRANT_API_KEY`,
`LLM_API_KEY`, and the other secrets in the deployment environment. Generate
distinct random values for `AUTH_JWT_SECRET`, `AUTH_BOOTSTRAP_TOKEN`, and
`INTELLIDOCS_INTERNAL_KEY`, for example with `openssl rand -hex 32` for each.
Core refuses missing, weak, and recognized placeholder auth secrets. Both
services require the same private internal key in production or demo mode.

Terminate HTTPS at the trusted ingress. Serve the web app and Core via one
HTTPS origin or same-site HTTPS origins. Set `VITE_API_URL` to the public Core
URL and `CORE_CORS_ORIGINS` to the exact web origins, with no wildcards.
Production uses a host-only `Secure`, `HttpOnly`, `SameSite=Strict` refresh
cookie. Keep Core's plaintext listener, Intelligence, Postgres, and Qdrant on
the private network. Only Core may send the internal key and authenticated
actor header to Intelligence. Set `CORE_TRUST_PROXY_HOPS` only when requests
must traverse that number of trusted proxies and direct access is blocked.

## Limits and shutdown

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `LLM_ENABLED` | `true` | Set `false` and restart both services to stop new LLM requests. Search, storage, and indexing remain available. Uploads return a clear notice if extraction is deferred. |
| `DEMO_LLM_CALLS_PER_USER_DAY`, `DEMO_LLM_CALLS_GLOBAL_DAY` | `20`, `100` | Counts actual provider attempts, including retries and failed calls. Cache hits spend no allowance. Use a global cap below the provider's daily quota to leave capacity for other consumers and differences in billing windows. |
| `DEMO_UPLOADS_PER_USER_DAY`, `DEMO_UPLOADS_GLOBAL_DAY` | `5`, `30` | Counts authenticated, admitted requests before multipart parsing, including submissions that later fail validation. |
| `DEMO_MAX_CONCURRENT_UPLOADS` | `2` | Caps admitted uploads in a Core process. |
| `MAX_UPLOAD_BYTES`, `MAX_UPLOAD_PAGES` | `20971520`, `25` | Cumulative byte and page limits for uploads with attachments. PDFs are checked before OCR. Other formats use the parser's logical pages. Production and demo uploads require `Content-Length`; configure equivalent or smaller limits at ingress. |
| `CORE_MAX_JSON_BYTES`, `INTELLIGENCE_MAX_JSON_BYTES` | `32768`, `524288` | JSON body limits in bytes. Authentication also has its own 8 KiB body limit. |
| `DEMO_RETENTION_DAYS`, `DEMO_SWEEP_INTERVAL_SECONDS` | `7`, `3600` | Periodic cutoff and sweep interval for demo uploads and cached LLM responses. |

Daily quotas reset at midnight UTC. A quota of zero denies that operation.
Quota and admission denials return a clear HTTP 429. Disabled LLM endpoints
return a clear HTTP 503. An already admitted request can finish during restart.

Upload counts are stored in Postgres. Provider counts are stored in SQLite
under Intelligence's persistent `STORAGE_DIR`. Run one Intelligence replica
on a persistent local volume. Independent or ephemeral volumes cannot enforce
one daily global provider limit across instances or restarts. Configure the
upstream provider's own budget limits where available.

The retention job selects uploads marked as demo data, including attachments,
and removes their database records, source files, and vectors. It retries
partial cleanup. Older uploads without the demo marker remain in place.
Append-only audit events retain metadata without retaining new demo
before/after payloads. Demo LLM caches live under `STORAGE_DIR/demo-cache` and
are swept separately by file age.

## Health and logs

`GET /health` is process liveness on each service. `GET /ready` returns HTTP
503 until dependencies are ready. Core checks Postgres and Intelligence.
Intelligence checks Qdrant connectivity and whether the configured provider has
a key. It never sends a readiness request to an LLM. A disabled provider or a
local fake provider does not require a key.

Use the image entry point or `python -m intellidocs_intelligence.serve` for
Intelligence. It disables raw access logs and exception messages. Both
services emit JSON request events with status and a correlation ID. Do not
configure ingress access logs to record query strings, bodies, or headers.

## Seed demo accounts

After bootstrapping a trusted administrator for each target organization,
set `DEMO_SEED_ACCOUNTS_JSON` privately to an array of objects with `org_id`,
`email`, `name`, `password` and an optional `role`. Passwords must be at least
12 characters. With no role supplied, the command uses the organization's
configured registration role. Roles that can register users or manage roles
are rejected. The command leaves existing accounts and their passwords alone.

Run from the Core directory after building:

```sh
npm run seed:demo
```

You can also run that command in the built Core container with the seed secrets
in its environment. Do not commit or print the seed JSON.
