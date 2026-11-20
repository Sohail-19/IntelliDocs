# Audience summaries

`SummaryService.summarize(SummaryRequest(...), assigned_obligations=...)` uses
the stored ParseResult, the configured retriever, and an audience snapshot
supplied by its caller. `summarizer_from_env(storage)` builds the service with
the normal index, model provider, verifier, and cache settings. The caller
resolves current assignments for the requested audience and supplies their
active, authorized `Obligation` v1 records with effective actions, due dates,
and citations. Core owns assignment state and any human overrides. Pass an
explicit empty list when the audience has no assigned obligations.

The service restricts retrieval to the document and its organization. It
queries with the selected profiles' topics, responsibilities, glossary terms,
and aliases. Matching stored obligations also inform the query. A normalized
phrase match against the profile vocabulary gates the ranked chunks, so dense
retrieval alone cannot add an unrelated passage. Include common paraphrases
and vocabulary in the org glossary for each source language.

Stored actions are copied verbatim with their resolved ISO due dates and
placed first after their citations are verified against the stored source.
Unresolved dates are omitted. Stored actions retain their stored language.
The LLM writes additional points in the request language and preserves the
source-language quotes. Citation verification requires exact normalized
quotes and valid offsets. With `VERIFICATION_SUPPORT_CHECK=true`, generated
points also pass the verifier's support check. The length budget covers point
text, and assigned obligations take priority if they exhaust that budget.

When no verified point remains, the result sets `not_relevant=true` and uses
the requested language's `summary.not_relevant_reasons` text when configured.
The fallback reason code is `no_verified_audience_points`. The two summary
prompt files live under `prompts/`. Invalid JSON is retried using
`GENERATION_MAX_RETRIES`. Exhausted retries return the already verified stored
points, or an empty result, and yield no cache entry.

Successful results use the existing disk cache under the `summaries`
subdirectory of `LLM_CACHE_DIR` or `STORAGE_DIR/llm-cache`. The key includes
content hash, document, audience, language, and prompt version, plus the
current assignment snapshot and generation/verification settings. Changing a
stored due date or an audience profile invalidates the cached summary.
