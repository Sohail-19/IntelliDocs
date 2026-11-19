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
| `EXTRACTION_FUZZY_THRESHOLD` | 1.0 | Minimum normalized quote similarity. Use 1.0 for exact matching. A lower value enables fuzzy attribution. |
| `EXTRACTION_CONFIDENCE_WEIGHTS` | See below | JSON object with optional overrides for the six evidence weights. |

The per-document cap limits selected chunks. Each uncached batch may make
one request plus the configured retries. Validated results reuse the existing
LLM cache keyed by provider, model, prompt, and schema. The extractor logs how
many chunks missed the detection rules or fell beyond the cap. If a batch
exhausts its retries, the call raises `ExtractionError`. Successful batches
from that run remain cached.

The processor checks quotes after Unicode normalization and whitespace folding.
It rejects drafts when neither the exact match nor an enabled fuzzy match
locates a source span. It resolves owner and deadline wording only when that
wording appears in the canonical source quote. The
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
drafts separate.

## Evidence confidence

The obligation's `confidence_breakdown` records six additive contributions.
The extractor calculates these from verified source evidence and org rules
after the drafts have been deduplicated.

| Key | Default weight | Evidence |
| --- | ---: | --- |
| `quote_exact` | 0.25 | The draft exactly matches the canonical citation after normalization and whitespace folding. |
| `quote_fuzzy` | 0.12 | Fuzzy attribution verified the citation and no exact match contributed. |
| `owner_resolved` | 0.55 | The cited owner resolves to a configured department or role. |
| `deadline_resolved` | 0.10 | The cited deadline resolves to a calendar date. |
| `recognized_cue` | 0.05 | The canonical quote contains a configured cue for the chunk's language. |
| `cross_chunk_agreement` | 0.05 | Matching drafts of the same obligation type independently cite non-overlapping spans from different chunks. |

Each present signal earns its weight divided by the maximum attainable
weight. The maximum uses the exact quote weight and the four non-quote
weights. Exact and fuzzy credit are mutually exclusive. The contributions
sum to confidence on a 0–1 scale. Overrides accept numbers from 0 to 1 and
keep the exact weight strictly higher than the fuzzy weight. Scores are
deterministic evidence scores. Calibration against labeled data is a
separate evaluation step.
