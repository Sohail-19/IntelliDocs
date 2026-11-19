# Core Service

Node/Express/TypeScript service for platform orchestration and integration with the intelligence service.

## API reference

| Endpoint | Required permission or credential |
| --- | --- |
| `GET /health` | Public |
| `POST /api/auth/register` | Bootstrap token for the first account, then `users:register` |
| `POST /api/auth/login` | Organization, email, and password |
| `POST /api/auth/refresh` | Current refresh token |
| `GET /api/auth/me` | Valid access token |
| `POST /api/auth/logout` | Current refresh token |
| `PUT /api/users/:id/roles` | `users:roles` |
| `GET /api/users`, `PATCH /api/users/:id` | `users:roles` |
| `GET /api/organisation` | `org:read` |
| `POST /v1/upload` | `documents:upload` |
| `GET /api/documents` | `documents:upload` |
| `GET /api/documents/upload-options` | `documents:upload` |
| `GET /api/documents/:id` | `documents:upload` |
| `POST /api/documents/:id/index` | `documents:upload` |
| `GET /api/search` | `documents:search` |
| `GET /api/search/options` | `documents:search` or `documents:answer` |
| `GET /api/documents/:id/source` | Search or answer permission, visible task, or pending review access |
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

## Roles and permissions

Grants come from `access.role_permissions` in each org config. These are the
grants in the bundled example org.

| Permissions | Roles |
| --- | --- |
| `documents:search`, `documents:answer`, `obligations:read`, `tasks:read` | `admin`, `manager`, `officer`, `finance_officer`, `viewer` |
| `documents:upload`, `tasks:transition` | `admin`, `manager`, `officer`, `finance_officer` |
| `audit:read`, `org:read`, `obligations:extract`, `obligations:status`, `obligations:assign`, `obligations:review` | `admin`, `manager` |
| `users:register`, `users:roles` | `admin` |

## Authentication

Apply migrations before starting core. Configure distinct, randomly generated
`AUTH_JWT_SECRET` and `AUTH_BOOTSTRAP_TOKEN` values of at least 32 bytes. The
service seeds organizations, departments and roles at startup from the org
files. The filename (without its extension) is the organization ID. Each
enabled org defines its `access` settings and role permissions in its YAML.

- `POST /api/auth/register` takes `org_id`, `email`, `name`, `password` (12 or
  more characters), and optional `department`. The first account in an
  organization also supplies `bootstrap_token` and receives the configured
  bootstrap role. Subsequent registrations require a bearer token with
  `users:register` in that organization. The administrator can specify a list
  of configured `roles`; otherwise the configured registration role applies.
- `POST /api/auth/login` takes `org_id`, `email` and `password`. It returns
  `access_token`, `refresh_token`, `token_type`, and `expires_in`.
- `POST /api/auth/refresh` takes `refresh_token` and returns a replacement pair.
  Save the replacement before making another refresh request. The submitted
  token can be used only once.
- `GET /api/auth/me` returns the signed-in user's ID, organization ID, roles,
  and effective permissions for the supplied bearer token.
- `POST /api/auth/logout` takes `refresh_token`, revokes the corresponding
  refresh session, and returns 204. Issued access tokens expire on their
  normal short lifetime.

Set `CORE_CORS_ORIGINS` to the comma-separated browser origins allowed to
call core from web. An empty allowlist permits same-origin browser requests.

Use `Authorization: Bearer <access_token>` on document and obligation routes.
Access tokens default to 900 seconds and refresh tokens to seven days. Override
these with `AUTH_ACCESS_TTL_SECONDS` and `AUTH_REFRESH_TTL_SECONDS`. Auth
routes share a per-IP, per-process limit set by `AUTH_RATE_LIMIT_PER_MINUTE`
(default 15). Serve core over HTTPS when clients connect over a network.

## User and organisation oversight

`GET /api/users` lists active and inactive accounts in the caller's org
with the configured role and department options. Use `PATCH /api/users/:id`
with `{"department":"finance"}` or `{"department":null}` to change or clear
membership. Send `{"active":false}` to deactivate an account and revoke its
sessions. The existing role endpoint replaces the assigned role set. Core
requires at least one active account with user administration permission.

`GET /api/organisation` returns the loaded languages, OCR engine, departments,
document types, and routing policy and rules for the caller's org.

Uploads use the authenticated organization. Searches, answers, and obligation
reads are confined to it. Previously stored documents without a trusted
organization assignment remain outside authenticated routes. Upload them
again under the intended organization to make them available. Legacy user rows
are retained as inactive accounts.

## Documents

`GET /api/documents` lists the authenticated organization's documents.
`GET /api/documents/:id` returns metadata and index and extraction status
for one document. New uploads store department, document date, MIME type,
detected page languages, and page count. Older rows can have unknown
metadata. `GET /api/documents/upload-options` returns the organization's
departments, document types, configured defaults, and `MAX_UPLOAD_BYTES`.

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

`GET /api/search` accepts `query` and `top_k`, plus optional filters and
`debug=true`. Use flat filter parameters or `filters[field]` parameters.
Supported filters are `department`, `doc_type` (or `document_type`),
`language`, `doc_id`, and `doc_date` or inclusive `doc_date_from` and
`doc_date_to` in `YYYY-MM-DD` form.

```sh
curl -G http://localhost:3000/api/search \
  --data-urlencode 'query=monthly report deadline' \
  --data-urlencode 'top_k=5' \
  --data-urlencode 'department=legal' \
  --data-urlencode 'debug=true'
```

Core forwards the query to intelligence and validates the v1 response before
returning the `results` envelope. Invalid input returns 422. An unavailable
intelligence service or an invalid upstream response returns 502.

`GET /api/answer` accepts required `query`, `language`, and `top_k` parameters
plus the same filter forms. Use `filters[language]` to filter source language
separately from the requested answer language.

```sh
curl -G http://localhost:3000/api/answer \
  --data-urlencode 'query=monthly report deadline' \
  --data-urlencode 'language=en' \
  --data-urlencode 'top_k=5'
```

The typed client forwards to intelligence's `POST /v1/answer`. Core validates
the Answer contract and rejects unverified claims, missing citations, or answer
text outside the verified claim list. Valid abstentions pass through. Invalid
input returns 422; unavailable intelligence or invalid upstream output returns
502. Verification cannot be disabled through this route.

`GET /api/search/options` returns the authenticated organization's
department, document type, and language filters. Search and answer citations
can include page-local boxes for blocks that intersect the cited span.
`GET /api/documents/:id/source` streams an original PDF from intelligence
after checking the document belongs to the authenticated organization.
Accounts with task-only access can open sources for assignments visible to
them. Reviewers can open sources for pending obligations in their
organization. For non-PDF sources the route returns 415 and the viewer shows
the cited page and quote.

After `POST /v1/upload` indexes a document or attachment, core runs obligation
extraction and stores its results with an audit entry. Apply migrations before
running this flow. The upload response includes `extraction_results`. If
extraction fails, the upload returns 502 with `extraction_failed_document_ids`
and the successful index results. The document remains indexed and its
extraction status is recorded separately.

- `GET /api/obligations` lists stored obligations and accepts `doc_id`, `status`,
  `owner_department`, `due_before`, and `due_after`. Date bounds are exclusive
  and use YYYY-MM-DD. Obligations without a normalized date do not satisfy
  date filters.
- `GET /api/documents/:id/obligations` uses the external document ID and returns
  obligations with the document's extraction status and any error.
- `POST /api/documents/:id/obligations` reruns extraction for an indexed
  document. Success replaces that document's previous obligations in one
  transaction, including when the result is empty. Failure retains the last
  successful snapshot and records the failed step in the audit log.

## Obligation routing

Enable routing in an organization file under `config/orgs/` by setting
`routing_policy.min_confidence`, `routing_policy.review_role`, and structured
`routing_rules`. Higher priorities run first, with file order breaking ties.
The first rule whose conditions all match wins. Use exactly one empty
`conditions: {}` rule as the last rule evaluated.

Rules can match the obligation type, resolved owner department, stored
document type, keywords, or deadline proximity. All listed keywords must
occur in the action or trigger text after Unicode, case, and whitespace
normalization. `deadline_within_days` counts UTC calendar days from today
through the given limit, inclusive. It uses resolved dates, and past dates
fall outside the window. Documents uploaded before the routing migration
have no stored document type.

A rule can target an active user in the organization by email, a configured
role queue, or a configured department queue. Automatic routing requires a
verified obligation with a citation, a resolved owner, and confidence at or
above the configured minimum. Other extraction results enter the configured
review queue, including results whose target user is unavailable. The
example org uses the `manager` queue and a threshold of `0.5`.
With default evidence weights, an exact quote with a resolved owner and
deadline scores at least `0.90`. An unresolved owner scores at most `0.45`.
A rule's optional `escalation_after_days` stores the assignment's escalation
due time.

Extraction routes obligations in the same transaction as persistence. A
repeated result keeps an unchanged assignment and its original escalation
time. Re-extraction also keeps active manual assignments. A changed routing
decision closes the old automatic assignment. When an obligation disappears
from an extraction result, core retires it and closes its assignment while
retaining the history.

`POST /api/documents/:id/obligations/:obligation_id/route` explicitly reruns
routing for a stored obligation. It requires `obligations:assign`. It closes
the current assignment, records a new one, and returns it as `assignment`.
Obligation reads include the active assignment. Each new routing assignment
writes an `obligation.route` audit event.

## Manager review

`GET /api/obligations/review` lists pending obligations for the authenticated
organization. The optional `doc_id` filter uses the external document ID.
The response includes the current owner and deadline alongside
`extracted_owner`, `extracted_deadline`, and `extracted_status`. When the
extractor supplies `confidence_breakdown`, the response shows the individual
evidence contributions stored with the original confidence score.
`GET /api/obligations/review/options` returns the organization's departments
and roles, including each role's department when configured.

Use the review endpoint for a document and obligation to save an owner,
a due date, or both. For example, send
`{"owner":{"role":"finance_officer"},"due_date":"2026-11-20"}` to the
`PATCH` endpoint. The owner may name a configured `department`, a configured
`role`, or both when they agree. A role with a configured department supplies
it automatically. Send `"due_date":null` to clear the effective deadline.
The original extracted fields remain available alongside the human overrides.

Approval requires a source citation and a resolved owner. It routes
immediately using the overridden values and bypasses the automatic confidence
threshold. Rejection requires `{"reason":"…"}`, closes the active assignment,
and removes it from the task feed. Changes are audited, and re-extraction
preserves human overrides and decisions for the same obligation ID. If a
refreshed extraction no longer supplies a citation, its prior approval
returns to review.

## Obligation tasks

Apply the task workflow migration before starting core. Assigned obligations
enter the feed as `open`. Task status stays separate from extraction status
and survives re-extraction or reassignment of the same obligation.

`GET /api/tasks` requires `tasks:read` and returns `tasks` and
`available_scopes`. Each task includes its ID, obligation and document IDs,
action, assignee or queue name, resolved due date, overdue flag, permitted
user transitions, and source citations. The feed sorts by due date ascending,
with undated tasks last. Filter by `status`, `doc_id`, or inclusive calendar
dates `due_from` and `due_to` in YYYY-MM-DD. All visible assignments appear
by default. Use `scope=mine` for directly assigned tasks and regular role
queues, `scope=department` for the user's department queue, or `scope=review`
for the configured review role queue. Queue visibility follows the user's
current memberships.

`PATCH /api/tasks/:id/status` accepts `{"status":"acknowledged"}` and requires
`tasks:transition` and access to the active assignment. Pending obligations
use the manager review endpoints. Rejecting a task
requires a nonblank `reason`. Each successful change records the acting user
and the before and after state in the audit log.

| Current status | Allowed user transitions |
| --- | --- |
| `open` | `acknowledged`, `rejected` |
| `acknowledged` | `in_progress`, `rejected` |
| `in_progress` | `done`, `rejected` |
| `overdue` | `acknowledged`, `in_progress`, `rejected` |
| `done`, `rejected` | Terminal |

The task job runs on startup and repeats at `TASK_SWEEP_INTERVAL_SECONDS`
(default 60). It marks open tasks overdue once their resolved due date is
earlier than today's UTC date. An unfinished task whose assignment reaches
the rule's `escalation_after_days` deadline moves to the organization's review
role queue. Escalation closes the old assignment and preserves task status.
The job records overdue transitions and escalations as system audit events.

## Audit trail

Core returns a server-generated `X-Request-ID` on each request. Upload, index,
extraction, obligation status and assignment, registration, successful login,
refresh, and role changes write audit events in the transaction that commits
the corresponding core change. An upload can produce multiple events for its
pipeline steps and attachments, linked by the same request ID. Each event
records the actor, entity, affected fields before and after the change, and
the connection IP. Auth events contain session metadata without credentials
or token values. PostgreSQL rejects attempts to update, delete, or truncate
audit history. Previously recorded entries remain visible to the owning org
when the migration can recover that ownership.

`GET /api/audit` requires the org's `audit:read` permission, granted to admins
and managers in the example config. It returns `entries` and `next_cursor`,
newest audit ID first. Optional filters are `action`, `entity_type`, `entity_id`,
`actor_user_id` (numeric ID or `system`), and inclusive UTC calendar dates
`from` and `to` in YYYY-MM-DD. The `limit` defaults to 25 and accepts up to
100. Pass `next_cursor` as `before_id` for the next page.

The following explicit changes are audited:

- `POST /api/documents/:id/index` retries indexing an existing document and
  runs extraction after a successful index.
- `PATCH /api/documents/:id/obligations/:obligation_id/status` takes
  `{"status":"needs_review"}` to send an undecided obligation to review in a
  routing-enabled org. Review decisions use the dedicated endpoints.
- `PATCH /api/documents/:id/obligations/:obligation_id/assignment` takes
  `{"assigned_user_id":42}` or `null` in that field to clear the assignment.
  The user must be active and in the same org.
- `PUT /api/users/:id/roles` takes `{"roles":["viewer"]}` and replaces the
  account's roles. The caller needs `users:roles` in the same org.
- `PATCH /api/users/:id` records a department change or deactivation.

The database transaction covers core state and its audit event. Intelligence
calls run outside that transaction, so a core write failure can leave an
intelligence-side result available for retry.
