# Citation verification

Use `verifier_from_env().build_answer(drafts.claims, retrieved_chunks,
model=model_name, documents=parsed_documents_by_id)` to build the displayed
`Answer`. It constructs answer text from verified claims only; it never copies
an unfiltered model-written answer. No surviving claims means empty answer
text, empty claims, `abstained=true`, and a reason. `verify()` returns all claim
statuses for internal diagnostics, including unverified claims.

Chunk IDs must be unique within the retrieved set. Quotes first match after
canonical Unicode decomposition (NFD) and whitespace folding, without changing
case or punctuation. Normalized characters retain their original source spans,
so citations contain the original source quote and document-global Python
character offsets (`char_start` inclusive, `char_end` exclusive). Clause number
and heading path come from the chunk.

Single-page chunks provide their page directly. For multi-page chunks, supply
the stored parsed documents through `documents`: their canonical block spans
locate the quote's exact page range. Without those spans, a multi-page quote
is unverified. Supplied documents must match chunk text and offsets; missing,
inconsistent, ambiguous, or header/footer sources fail verification.

`VERIFICATION_FUZZY_THRESHOLD` defaults to `1.0`, requiring an exact normalized
match. Set a value in `(0, 1)` to enable a fallback using substring Levenshtein
similarity, `1 - edits / max(quote_length, matched_length)`. The citation still
quotes the matched original source text. Lower thresholds permit larger textual
differences; fuzzy similarity alone does not establish factual support.

`VERIFICATION_SUPPORT_CHECK=true` enables a second stage using the existing
LLM provider and disk cache settings. The file-based prompt supplies only the
claim and matched source quote, and accepts a strict `supported` / `not supported`
verdict. Invalid responses, missing providers, and provider/cache failures mark
the claim unverified. With the flag disabled (the default), verification checks
source attribution and positions only. Inject a provider and `VerificationConfig`
directly into `CitationVerifier` for tests or custom composition.
