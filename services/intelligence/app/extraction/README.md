# Obligation draft extraction

The service exposes `POST /v1/extract-obligations` with a `doc_id` JSON field.
It loads the stored parse result and the acknowledged index, then runs the
extractor and processor below. Configure the LLM provider for extraction calls.

Call `extractor_from_env(org).extract(chunks)` with the loaded OrgConfig and
chunks from one document. The detector returns chunks with at least one
matching sentence. The extractor sends those chunks in batches and returns
draft obligations with the source chunk ID and the quote produced by the model.
Pass the drafts to `ObligationProcessor(org).process(document, chunks, drafts)`
to get an `ExtractionResult`. Use the original ParsedDocument so the verifier
can check page and character ranges against canonical text.

In the org YAML, add `obligation_detection` entries keyed by a code in
`languages`. Each entry supports `cues` (literal phrases) and
`date_patterns` (Python regular expressions for dates and durations). The
example org includes English and Hindi rules. Matching folds case and
whitespace and applies canonical Unicode normalization. An exact language
tag or its base tag selects its rules. For an unknown or missing tag, the
detector tries all configured rules. ISO-style dates can also flag a candidate.

| Setting | Default | Purpose |
| --- | ---: | --- |
| `EXTRACTION_BATCH_SIZE` | 4 | Candidate chunks per provider request. |
| `EXTRACTION_MAX_CHUNKS_PER_DOCUMENT` | 20 | Unique candidate chunks selected in input order per run. Set 0 to skip provider calls. |
| `EXTRACTION_MAX_RETRIES` | 1 | Additional attempts per batch after invalid JSON or schema output. |

The per-document cap limits selected chunks. Each uncached batch may make
one request plus the configured retries. Validated results reuse the existing
LLM cache keyed by provider, model, prompt, and schema. The extractor logs how
many chunks missed the detection rules or fell beyond the cap. If a batch
exhausts its retries, the call raises `ExtractionError`. Successful batches
from that run remain cached.

The processor rejects obligations whose quotes fail exact matching after
Unicode normalization and whitespace folding. It resolves owner and deadline
wording only when that wording appears in the verified quote. The
`department_aliases` map lists alternate names for departments. The `roles`
map defines role names, their optional department, and spelling variants under
`aliases`. Owner matching folds Unicode, case, punctuation and whitespace.
Unknown owners and aliases shared by different targets require review.

Absolute dates use the configured languages and require a complete,
unambiguous calendar date. A language can use `absolute_deadline_wrappers`
to capture a date inside phrases such as “by …” or “… तक”. Each wrapper
matches the whole expression and has one named group, `date`.

`relative_deadline_patterns` also match whole expressions. Each has one named
capture for the duration, `days`, `weeks`, `months`, or `years`. A match uses
the document's `doc_date` as its anchor, including configured phrases such as
“within 30 days of receipt”. Patterns with an `unsupported` capture identify
relative deadlines the processor cannot resolve, such as business-day
deadlines. Missing anchors and unrecognized or ambiguous dates require review
when a deadline was stated. A genuinely absent deadline stays unresolved and
does not by itself require review.

Duplicates with the same action, owner, deadline and condition merge their
distinct citations. The processor keeps different conditions and rejected
drafts separate. Drafts carry no calibrated confidence score, so results use 0.
