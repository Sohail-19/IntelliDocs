# Deploy the public demo on free tiers

**Hugging Face requires a subscription to create a Docker Space, even on CPU
Basic.** The Docker Space instructions below apply if your account already
has access at no extra cost. For a new, zero-cost deployment, run Intelligence
as a second Render Free web service. Both paths keep Core on Render, vectors
in Qdrant Cloud, metadata and quotas in Neon, and the web app on Vercel.
[Hugging Face Spaces account requirements](https://huggingface.co/docs/huggingface_hub/guides/manage-spaces)

## Limits checked on 2026-11-20

| Provider | Verified allowance and deployment effect |
| --- | --- |
| Hugging Face Spaces CPU Basic | 2 vCPU, 16 GB RAM, 50 GB of non-persistent disk. CPU Basic has no hourly hardware fee. Docker Space creation requires a subscription. Default inactivity sleep is 48 hours. [Hardware and eligibility](https://huggingface.co/docs/hub/spaces-overview), [sleep behaviour](https://huggingface.co/docs/hub/spaces-gpus#sleep-time) |
| Render Free web services | 0.1 CPU and 512 MB RAM per service. Idle spin-down after 15 minutes and about a minute to wake. The workspace shares 750 free instance hours per calendar month. Current Hobby includes 5 GB outbound bandwidth and 500 build minutes per month. Local files are ephemeral. Free services have no shell or one-off jobs. Keep the workspace without a payment method so overages pause services or builds. [Free services](https://render.com/docs/free), [bandwidth](https://render.com/docs/outbound-bandwidth), [build minutes](https://render.com/docs/build-pipeline), [instance sizes](https://render.com/docs/blueprint-spec#plan) |
| Qdrant Cloud Free | One node with 0.5 vCPU, 1 GB RAM and 4 GB disk. Suspends after one week of inactivity and deletes after four weeks of inactivity if not reactivated. [Free clusters](https://qdrant.tech/documentation/cloud/create-cluster/) |
| Neon Free | 1 GB of Postgres storage per project, 100 CU-hours and 5 GB public network transfer per project per month. Compute sleeps after five idle minutes. [Neon plans](https://neon.com/docs/introduction/plans) |
| Vercel Hobby | Personal, non-commercial use. Includes 1 million edge requests and 100 GB fast data transfer per month. External rewrites have a 120-second request timeout. [Hobby terms](https://vercel.com/docs/plans/hobby), [pricing](https://vercel.com/pricing), [proxy limits](https://vercel.com/docs/limits#proxied-request-timeout) |
| Netlify Free, alternative web host | 300 monthly credits with a hard cap. Production deploys use 15 credits each, bandwidth uses 20 per GB, and 10,000 web requests use 2. External proxy rewrites time out after 26 seconds. This guide uses Vercel so the proxy has time to wait for a cold backend. [Netlify plans](https://docs.netlify.com/manage/accounts-and-billing/billing/billing-for-credit-based-plans/credit-based-pricing-plans/), [proxy limits](https://docs.netlify.com/manage/routing/redirects/rewrites-proxies/) |

The free profiles keep `LLM_ENABLED=false`. The current LLM meter requires
persistent local storage and its counters would reset on ephemeral hosting.
Use synthetic demo uploads. Neon retains metadata, and Qdrant retains vectors
and excerpts. Original files and parsed results on the service disk can
disappear after a restart, so older source links can stop working. Upload a
fresh fixture for each smoke test. With two Render services, both consume the
same 750 workspace hours. Let the services idle between sessions.

## 1. Create accounts and prepare the repository

1. Create accounts in [Render](https://dashboard.render.com/),
   [Qdrant Cloud](https://cloud.qdrant.io/), [Neon](https://console.neon.tech/)
   and [Vercel](https://vercel.com/). Use Vercel Hobby for a personal,
   non-commercial demo. If you already have Docker Space access, sign in to
   [Hugging Face](https://huggingface.co/).
2. Push the deployment files to your Git provider. For Vercel Hobby Git import,
   use a repository under your personal Git account. Connect that repository
   to Render and Vercel. Use the same commit for the deployment and migrations.
3. Use Bash for the commands below. Have Git, `curl`, OpenSSL, Python 3.11,
   and Node 20 or 22 available locally. For the Docker Space route, also
   install the [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/guides/cli).
4. In a private terminal generate the shared service key with
   `export INTELLIDOCS_INTERNAL_KEY="$(openssl rand -hex 32)"`. Put that value
   in the Intelligence and Core dashboards as described below. Keep it out of
   commit history and client-side environment variables.

**Expected** — Your Git repository appears in both import screens, and you
have one private service key for the two backends.

## 2. Create Qdrant and Postgres

1. In **Qdrant Cloud → Clusters → Create**, select **Free** and a nearby
   region. Copy the cluster's HTTPS endpoint including its port. In the
   cluster's **API Keys** screen, create a database API key that can list and
   create collections and read/write points. Set it aside for Intelligence.
   Intelligence creates its collection on the first index request.
   [Qdrant Cloud setup](https://qdrant.tech/documentation/cloud-getting-started/)
2. In the **Neon Console**, create a project on **Free** in a nearby region.
   Click **Connect** and copy two URLs for the same database and role. With
   **Connection pooling** enabled, copy the pooled URL for Core. Turn it off
   and copy the direct URL for migrations and seed. Keep the TLS parameters
   included in the connection strings. [Neon connection dialog](https://neon.com/docs/connect/connect-from-any-app)

**Expected** — Qdrant shows a running Free cluster. Neon provides two
`postgresql://` URLs for the same project. The pooled hostname contains `-pooler`.

## 3. Deploy Intelligence

### Zero-cost route for a new account

In **Render → New → Blueprint**, connect the repository. Enter
`render.intelligence.yaml` in **Blueprint Path**. Review the single Free web
service, enter the Qdrant URL, Qdrant key and shared internal key when prompted,
then choose **Deploy Blueprint**. The image uses Render's injected `PORT`.
It uses BM25 for search and a deterministic dense placeholder for indexing,
so it avoids the neural model load. E5-small's measured indexing run peaked
near 1 GiB, above Render Free's 512 MB. [Local model comparison](performance.md)
[Custom Blueprint paths](https://render.com/docs/infrastructure-as-code), [Render port binding](https://render.com/docs/web-services#port-binding)

### Docker Space route for an account with access

1. On Hugging Face choose **New Space**, **Docker**, **Blank**, **CPU Basic**,
   and **Public**. Only ship code, schemas, and synthetic configuration.
   Core authenticates to the public API host using the shared service key.
2. In the Space's **Settings → Variables and secrets**, enter the values in
   the table below under **Space**. `APP_UID=1000` follows Docker Spaces'
   runtime user requirement. `PROVISION_E5_SMALL=true` downloads the pinned
   public weights during the Docker build. `EMBEDDING_LIGHTWEIGHT=true`
   selects them at runtime. [Docker Space variables and permissions](https://huggingface.co/docs/hub/spaces-sdks-docker)
3. From a checkout containing the committed deployment files, publish the
   selected files to the Space. Authenticate Git privately first.

   ```bash
   hf auth login --add-to-git-credential
   space_checkout=$(mktemp -d)
   git clone https://huggingface.co/spaces/YOUR_NAMESPACE/intellidocs-intelligence "$space_checkout"
   git archive HEAD .dockerignore LICENSE config contracts services/intelligence | tar -x -C "$space_checkout"
   cp services/intelligence/Dockerfile "$space_checkout/Dockerfile"
   cp services/intelligence/README.spaces.md "$space_checkout/README.md"
   git -C "$space_checkout" add .dockerignore LICENSE config contracts services/intelligence Dockerfile README.md
   git -C "$space_checkout" commit -m 'chore: publish intelligence space'
   git -C "$space_checkout" push origin HEAD:main
   ```

   The copied README sets `sdk: docker` and `app_port: 8000`. Use the app
   origin ending in `.hf.space`, which you can find via **Embed this Space**.
   [Space header reference](https://huggingface.co/docs/hub/spaces-config-reference)

**Expected for either route** — Run `export INTELLIGENCE_URL=https://YOUR_INTELLIGENCE_HOST`
using the public origin shown by your host.
`curl -fsS "$INTELLIGENCE_URL/health"` returns `{"status":"ok"}`.
`curl -fsS "$INTELLIGENCE_URL/ready"` reports `status: ready`, `qdrant: ok`, and
`llm: disabled`. Those checks do not load embeddings. In the Space profile the
first upload or search loads E5 from disk for that process. A sleeping host
must first start the container. Let both backends report ready before sending
an upload, and allow extra time for the first E5 request. The Render fallback
has no neural-model load. See the [measured model comparison](performance.md).

## 4. Deploy Core

In **Render → New → Blueprint**, select the repository and leave **Blueprint
Path** as `render.yaml`. Verify the Free plan. Fill in `DATABASE_URL` with the
pooled Neon URL, `INTELLIGENCE_URL` with the origin from step 3, and
`INTELLIDOCS_INTERNAL_KEY` with the same shared key. Set `CORE_CORS_ORIGINS`
to the intended `https://YOUR_PROJECT.vercel.app` origin. Render generates
separate JWT and bootstrap secrets. Click **Deploy Blueprint**.

**Expected** — The Core service has an `https://…onrender.com` origin. Its first
launch can fail while the database is empty. Run the next step before
retrying it. Render supplies `$PORT`, and Docker starts the built server.
Automatic deploys are off so migrations can run before future releases.

## 5. Run migrations and seed the demo user

Run this once per release from your local checkout with Node 20 or 22. Use the
**direct** Neon URL. The migration tool records applied migrations and uses a
database lock. Run one migration command at a time. For future updates,
migrate before clicking **Manual Deploy → Deploy latest commit** on Core. The
service startup command does not apply migrations.

```bash
read -rsp 'Direct Neon URL: ' NEON_DIRECT_URL; echo
export NEON_DIRECT_URL
npm --prefix services/core ci
DATABASE_URL="$NEON_DIRECT_URL" npm --prefix services/core run migrate:up
npm --prefix services/core run build
```

On the first deployment, click **Manual Deploy → Deploy latest commit** on
Core after migration. Set `CORE_URL` in your terminal to its public origin.
Wait for `curl -fsS "$CORE_URL/ready"` to report Postgres and Intelligence
as `ok`. From **Core → Environment**, reveal `AUTH_BOOTSTRAP_TOKEN` privately.
Create the first administrator in the bundled `example` org if it has none.

```bash
export CORE_URL=https://YOUR_CORE.onrender.com
read -rsp 'Core bootstrap token: ' AUTH_BOOTSTRAP_TOKEN; echo
read -rsp 'New administrator password (12+ chars): ' ADMIN_PASSWORD; echo
export AUTH_BOOTSTRAP_TOKEN ADMIN_PASSWORD
python3 - <<'PY' | curl -fsS "$CORE_URL/api/auth/register" -H 'Content-Type: application/json' --data-binary @-
import json, os
print(json.dumps(dict(org_id='example', email='admin@example.test', name='Demo operator',
                      password=os.environ['ADMIN_PASSWORD'],
                      bootstrap_token=os.environ['AUTH_BOOTSTRAP_TOKEN'])))
PY
unset AUTH_BOOTSTRAP_TOKEN ADMIN_PASSWORD
```

Seed the smaller-scope login using an access role from the org config. In the
bundled `example` org, `finance_officer` can upload and search. Use unique,
non-reused credentials and keep the operator login private.

```bash
export SMOKE_ORG_ID=example SMOKE_EMAIL=pilot@example.test
read -rsp 'New demo user password (12+ chars): ' SMOKE_PASSWORD; echo
export SMOKE_PASSWORD
DEMO_SEED_ACCOUNTS_JSON=$(python3 - <<'PY'
import json, os
print(json.dumps([dict(org_id=os.environ['SMOKE_ORG_ID'], email=os.environ['SMOKE_EMAIL'],
                       name='Demo user', role='finance_officer', password=os.environ['SMOKE_PASSWORD'])]))
PY
) DATABASE_URL="$NEON_DIRECT_URL" npm --prefix services/core run seed:demo
unset NEON_DIRECT_URL
```

**Expected** — Migrations finish successfully, the first registration returns
HTTP 201, and the seed prints `demo_accounts_seeded` with `created: 1`. Rerunning
the seed for an existing user returns `created: 0` and keeps their password.

## 6. Deploy the web app

1. In **Vercel → Add New → Project**, import the personal repository on Hobby.
   Keep **Root Directory** at the repository root so the app can import
   `contracts/`. Select **Other** if the wizard asks for a framework and use
   Node 22 or later. The root `vercel.ts` installs and builds `apps/web`.
2. Add `CORE_PROXY_URL` with the public Core HTTPS origin under **Environment
   Variables** for Production, then deploy. The build sets `VITE_API_URL=/`.
   The browser sends API requests through the web origin, which keeps the
   `Secure`, `HttpOnly`, same-site refresh cookie on that origin.
3. Run `export WEB_URL=https://YOUR_PROJECT.vercel.app` with the actual Vercel
   production origin. If it differs from the provisional origin, set
   **Core → Environment → CORE_CORS_ORIGINS** to the exact final HTTPS origin
   and use **Save and deploy**. Use the production alias for sign-in. Separate
   preview origins need their own allowlist entries.
   [Vercel programmatic config](https://vercel.com/docs/project-configuration/vercel-ts), [external rewrites](https://vercel.com/docs/routing/rewrites), [Render environment variables](https://render.com/docs/configure-environment-variables)

**Expected** — `https://YOUR_PROJECT.vercel.app/login` shows IntelliDocs. A
successful sign-in leaves a host-only refresh cookie on the web origin, and
the access token stays in browser memory.

## 7. Smoke test

From your checkout, export `SMOKE_ORG_ID`, `SMOKE_EMAIL`, and `SMOKE_PASSWORD`
for the demo login. Export the deployed Intelligence, Core and web origins.

```bash
scripts/smoke_test.sh "$INTELLIGENCE_URL" "$CORE_URL" "$WEB_URL"
unset SMOKE_PASSWORD INTELLIDOCS_INTERNAL_KEY
```

**Expected** — The script checks liveness and readiness on both APIs and opens
`/login`. It signs in via the production web proxy, rotates the refresh cookie,
uploads one original fixture PDF, waits for indexing, and checks that search
finds that document. Each run uses one demo upload. If a host is waking, the
health polls allow up to four minutes per check by default. Set
`SMOKE_WAIT_SECONDS` to change that wait. An upload is attempted only once.

## Environment variables by service

| Variable | Location | Value or source |
| --- | --- | --- |
| `INTELLIDOCS_ENV`, `DEMO_MODE`, `LLM_ENABLED` | Both APIs | `production`, `true`, `false` in the free profiles. |
| `INTELLIDOCS_INTERNAL_KEY` | Both APIs, secret | Same random key, shared between backends only. |
| `INTELLIGENCE_PORT`, `APP_UID`, `PROVISION_E5_SMALL` | Space variables | `8000`, `1000`, `true`. `/health` is the Space's landing path. Leave `CORE_PORT` and `INTELLIGENCE_PORT` unset on Render to use its injected `PORT`. |
| `TESSERACT_LANGUAGES`, `OMP_THREAD_LIMIT`, `OPENBLAS_NUM_THREADS` | Intelligence | `eng,hin,spa,fra`, `1`, `1`. These packs support the bundled configs. |
| `EMBEDDING_LIGHTWEIGHT`, `EMBEDDING_LIGHTWEIGHT_MODEL` | Space variables | `true`; the image sets the weights path to `/opt/models/multilingual-e5-small`. |
| `EMBEDDING_BACKEND`, `EMBEDDING_LIGHTWEIGHT`, `RETRIEVAL_MODE` | Render Intelligence | `fake`, `false`, `sparse`. On the Space use `RETRIEVAL_MODE=hybrid`. |
| `SPARSE_EMBEDDING_BACKEND`, `RERANKER_BACKEND`, `EMBEDDING_BATCH_SIZE` | Intelligence | `bm25`, `none`, `8` for either host. |
| `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION` | Intelligence | Qdrant HTTPS endpoint, database API key as a secret, and `intellidocs_demo_bm25_v1` on Render or `intellidocs_demo_e5_v1` on the Space. Use a fresh collection whenever the embedding layout changes. |
| `STORAGE_DIR`, `ORG_CONFIG_DIR` | Intelligence | Docker defaults `/data/uploads` and `/config/orgs`. |
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_BASE_URL` | Intelligence | Leave unset for the free profiles with `LLM_ENABLED=false`. |
| `MAX_UPLOAD_BYTES`, `DEMO_RETENTION_DAYS`, `DEMO_SWEEP_INTERVAL_SECONDS` | Both APIs | `1048576`, `7`, `3600`. |
| `MAX_UPLOAD_PAGES` | Intelligence | `4` total, including attachments. |
| `DATABASE_URL`, `DATABASE_POOL_MAX`, `DATABASE_CONNECT_TIMEOUT_MS` | Core | Pooled Neon URL as a secret, `3`, `10000`. |
| `INTELLIGENCE_URL`, `CORE_CORS_ORIGINS` | Core | Public Intelligence origin and exact Vercel origin. Multiple permitted origins are comma separated. |
| `AUTH_JWT_SECRET`, `AUTH_BOOTSTRAP_TOKEN` | Core, generated secrets | Separate Render-generated values. |
| `DEMO_UPLOADS_PER_USER_DAY`, `DEMO_UPLOADS_GLOBAL_DAY`, `DEMO_MAX_CONCURRENT_UPLOADS`, `TASK_SWEEP_INTERVAL_SECONDS` | Core | `3`, `12`, `1`, `3600`. Sweep jobs run while the service is awake. |
| `CORE_PROXY_URL`, `VITE_API_URL` | Web build | Render Core origin, and `/` set by `vercel.ts`. Browser builds receive no service or database secrets. |
| `DATABASE_URL`, `DEMO_SEED_ACCOUNTS_JSON` | Local release terminal | Direct Neon URL for migrations and seed; private account JSON for the seed only. |
| `SMOKE_ORG_ID`, `SMOKE_EMAIL`, `SMOKE_PASSWORD`, `SMOKE_WAIT_SECONDS` | Local smoke test | Demo credentials and optional poll wait. |

The [public demo safeguards](public-demo.md) guide describes quota accounting,
HTTPS assumptions, and retention details. Recheck the free allowances before
sharing the demo widely.
