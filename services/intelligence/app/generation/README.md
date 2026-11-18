# Draft claim generation

`generator_from_env().generate(query, chunks)` uses the existing LLM provider
and disk cache. Pass retrieved `Chunk` models; only their IDs and text enter the
file-based prompt alongside the question. The result contains `claims`, each
with `text`, `chunk_id`, and `quote`. An empty evidence set skips the provider.

`GENERATION_MAX_RETRIES` defaults to `2`: one initial attempt plus up to two
retries for malformed JSON or an invalid output schema. Exhaustion raises
`GenerationError`; a valid empty claims list is a normal result. Transport
errors propagate to the caller.

Only schema-valid responses are cached, including successful retries. Cache
keys use the provider, model, original prompt, and output schema. Set
`LLM_CACHE_DIR` to override the default `STORAGE_DIR/llm-cache`. Cached responses
also pass schema validation before being returned. For dependency injection,
construct `ClaimGenerator(provider, config, cache)` directly.

These are draft claims. Source attribution and quote verification belong to
the later verification stage.
