# Onboard an organization

An organization is selected by the signed-in account. Its slug is the filename
under `config/orgs/`, without the extension. `example.yaml` models a legal and
finance office in English and Hindi. `example2.yaml` models a water utility in
Spanish and French, with chlorine monitoring, service interruption notices and
separate quality and customer-service teams.

## Checklist

1. Create `config/orgs/<slug>.yaml`. Use a unique slug with letters, numbers,
   `_`, or `-`. List the supported ISO 639-1 `languages` in preference order.
   The first language is the default for the document summary. Choose the
   `ocr_engine` and make its local language resources available. For Tesseract,
   the example utility needs `spa` and `fra` for image OCR. If building the
   Docker image, include both in `TESSERACT_LANGUAGES`. Digital email and Word
   fixtures use text extraction and local language detection.
2. Define canonical `document_types`, `departments`, and `roles`. Add known
   owner aliases and optional upload defaults. Set `document_structure` labels,
   page-number patterns, and sentence abbreviations for the source vocabulary.
3. Define `access` with a bootstrap role, a registration role, and their grants.
   Grant `documents:summary-preview` to reviewers who can preview other
   audiences. Configure `routing_policy` and ordered `routing_rules`, including
   one unconditional rule with the lowest priority. Reference declared queues.
4. Add per-language `obligation_detection` cues and deadline patterns. Relative
   patterns capture exactly one named group from `days`, `weeks`, `months`,
   `years`, or `unsupported`. Absolute wrappers capture `date`. Add relation
   phrases under `relation_detection`. Define `summary` profiles for departments
   and roles, including local glossary aliases and relevance messages.
5. Validate the file from the repo root after installing the Intelligence
   development environment. The command accepts multiple files, reports paths
   for schema errors and invalid references, and exits nonzero for any invalid
   config.

   ```sh
   services/intelligence/.venv/bin/python scripts/validate_org_config.py config/orgs/example2.yaml
   ```

6. Apply the Core database migrations with `npm --prefix services/core run
   migrate:up`, then start Core and Intelligence with the same org config
   directory. Core seeds the organizations, departments, and roles on startup.
   To start using the new organization, register its first account with its slug
   and the configured bootstrap token. Sign in with that slug and use its admin
   grants to invite users and assign roles.
7. Upload a representative document with a known document date. Check its
   detected language, the evidence span, relative deadline resolution, and the
   destination role or department queue. Sign in as two readers to verify each
   receives the appropriate **For you** summary. Compare search and the audit
   view using another organization's account.

## Reproduce the two-organization test

Install the Core test dependencies and Intelligence development environment as
shown in the root README. Run this from the repo root:

```sh
ONBOARDING_INTELLIGENCE_PYTHON="$PWD/services/intelligence/.venv/bin/python" \
  npm --prefix services/core test -- tests/onboarding.test.ts
```

The test uses both YAML files on one Core and Intelligence instance with
PGlite, in-memory Qdrant, temporary document storage, and deterministic model
responses. It runs the real email parser, extraction, owner and date resolution,
routing, summary, and citation checks. The Spanish fixture is routed to the
quality inspector and yields a separate notice for the resident liaison.
The independent isolation test runs in the standard Core suite and checks
known IDs and lists for documents, obligations, tasks, relations, and audit.

Core scopes the browser APIs to the authenticated account's organization and
passes that scope to the internal Intelligence API. Qdrant uses the tenant payload filter for
retrieval and document operations. A search without a tenant filter is limited
to legacy points with no organization. New documents, settings, and child
records are tenant-owned. The ownership migration derives ownership from
existing parent rows where possible. Historical rows without an attributable
organization remain outside authenticated views.
