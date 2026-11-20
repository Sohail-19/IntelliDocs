# Architecture

IntelliDocs separates document evidence from workflow state. Intelligence interprets files and returns cited results. Core decides what the caller can see and what workflow action to take. The web app calls Core and renders the resulting documents, citations, tasks, and review decisions. [Repository map](../README.md#feature-to-code-map)

## From upload to task

1. **Accept and parse.** A signed-in user uploads a file through Core. Core checks permission, requires any supplied organization to match the account, and forwards the file and authorized organization to Intelligence. The parser selected by org config produces pages, languages, reading order, blocks, clause labels, and structured tables. Email attachments become linked documents. Intelligence saves the original files and v1 `ParseResult` JSON. Core records document metadata and processing status in Postgres.
2. **Index.** Core requests indexing for the document and each parsed attachment. Intelligence builds structural chunks, preserves offsets into canonical text, and upserts their vectors and metadata in Qdrant. Unchanged content can reuse embeddings when the chunking and model settings still match. Core records the indexing outcome.
3. **Extract and verify.** Intelligence uses configured cues and date patterns to select candidate chunks for the model. It verifies the returned quotes, resolves owners against aliases and roles, and uses an explicit document date to anchor supported relative deadlines. It keeps raw owner and deadline text, merges duplicate obligations, and returns citations and an evidence-based confidence breakdown. Core stores the extraction and records it in the audit trail. An extraction deferred by the LLM kill switch or quota leaves an indexed document available for search.
4. **Route or review.** Core applies the configured confidence threshold and ordered routing rules. Verified obligations with resolved owners and sufficient confidence can route to a user, role queue, or department queue. Other obligations go to review. Managers with the review permission can correct an owner or deadline, retain the original extracted values, and approve or reject. Approval routes immediately.
5. **Work and audit.** The task feed exposes authorized active assignments with due dates, permitted transitions, and citations. Core enforces workflow transitions and requires a reason for rejection. Task changes and associated audit entries commit together. The scheduled job marks overdue work and escalates eligible assignments.
6. **Handle changes in authority.** After extraction, Core can ask Intelligence to detect relations with earlier documents using evidence from both sides. Manager confirmation applies document or clause authority changes and flags affected open obligations and tasks. Stale tasks are excluded from automatic escalation pending a manager decision. Search and summaries carry authority warnings, and Core suppresses stale-cited claims from current answers.

[Upload orchestration](../services/core/src/upload.ts) · [Routing](../services/core/src/routing/) · [Task workflow](../services/core/src/tasks/) · [Relation review](../services/core/src/relations/)

## Service boundary and contracts

| Component | Responsibility |
| --- | --- |
| Web | React UI, permissions-driven navigation, upload and task views, and source viewing. Uses the [Core API types](../contracts/core-api.v1.d.ts). |
| Core | Express API, JWT sessions, authorization, workflow orchestration, and the append-only audit trail. Owns Postgres and calls Intelligence through a [typed client](../services/core/src/intelligence/client.ts). |
| Intelligence | FastAPI ingest, indexing, search, grounded answers, obligation extraction, relation detection, and audience summaries. Owns original files, parsed results, and Qdrant vectors and chunk payloads. |

Cross-service requests and responses use only the versioned interfaces in [`contracts/`](../contracts/). The [Intelligence OpenAPI document](../contracts/openapi/intelligence.v1.yaml) refers to v1 JSON Schemas. Python models and TypeScript types implement that boundary. The services exchange HTTP payloads and operate their own stores.

Search returns cited chunks from the authorized collection scope. Answer and summary generation use retrieved or selected document passages, and the verifier checks each supporting quote before the result is displayed. Core supplies audience context and stored obligations for document summaries. A citation records the document, inclusive one-based pages, and zero-based, end-exclusive Unicode code-point offsets in the canonical parsed text. Clause and heading context and PDF block boxes may be available. The source viewer can show the quote when boxes or a PDF are unavailable. [Contract details](../contracts/README.md)

## Multi-organization isolation

- **Identity.** Login selects an organization and Core derives the active scope from the authenticated account. Org configuration defines its roles and grants. Core constrains list queries and known-ID reads and mutations to that scope, including documents, obligations, tasks, relations, and audit.
- **Relational state.** Postgres is shared. Tenant ownership columns, same-org foreign keys on key relationships, and Core's scoped repository queries enforce ownership. The tenant migration backfills ownership where it can derive it. Historical records with no attributable tenant remain outside authenticated views.
- **Files and vectors.** Core sends the trusted org slug to Intelligence. Document operations check the org on the stored parse result. Qdrant search and document operations use exact `org_id` payload filters, and vector point IDs include the organization scope. Unscoped legacy retrieval considers only points without an org ID.
- **Network boundary.** The browser reaches Core. Production Intelligence requires the shared internal key on its `/v1/` routes. Treat direct access to the databases, storage, or that key as privileged. The isolation model uses scoped access in shared infrastructure.

[Tenant ownership migration](../services/core/migrations/1700000000012_tenant_ownership.ts) · [Vector storage](../services/intelligence/app/vector_store/) · [Two-organization test instructions](onboarding.md#reproduce-the-two-organization-test)

## Configuration model

Use one [OrgConfig YAML](../contracts/schemas/org-config.v1.schema.json) per organization under [`config/orgs/`](../config/orgs/). The filename is the organization slug. Both services load the same configurations. Core validates them and seeds configured organizations, departments, and roles at startup. Intelligence uses the selected org to parse, extract, detect relations, and summarize.

YAML defines languages, OCR engine, document types and defaults, departments, roles, grants, aliases, cue and deadline vocabulary, relation detection, routing conditions and priorities, and audience profiles. Deployment settings such as paths, ports, model backends, credentials, and quotas come from environment variables. Provision any required OCR language packs and optional model files for the chosen runtime.

[Onboard a new organization](onboarding.md) · [Validate a configuration](../scripts/validate_org_config.py) · [Deployment environment variables](deploy.md#environment-variables-by-service)
