# Structural chunking

```python
from app.chunking import Chunker, ChunkingConfig, canonical_document

settings = ChunkingConfig.from_env()
source = canonical_document(parse_result.document)
chunks = Chunker(settings).chunk(parse_result.document)
for chunk in chunks:
    assert source.text[chunk.char_start : chunk.char_end] == chunk.text
```

Configuration is explicit: `CHUNK_TARGET_TOKENS`, `CHUNK_OVERLAP_TOKENS`, and
`CHUNK_TOKEN_COUNT_METHOD` are all required by `from_env`. `.env.example` supplies
example values. Alternatively construct `ChunkingConfig` with values loaded by
your caller from configuration. The Python implementation has no implicit size,
overlap, or counter defaults. Target must be positive and overlap must be
nonnegative and smaller than target.

Supported counters are `whitespace` (whitespace-delimited tokens), `unicode_words`
(Unicode word runs and individual non-whitespace punctuation/marks), and
`characters` (Unicode code points including whitespace). These are deterministic
size measures, not an embedding model's subword tokenizer. Counter selection does
not alter the text or its offsets. No model download or embedding call is involved.

## Canonical text v1 and citations

`canonical_document` sorts pages by page number and blocks by `reading_order`.
Duplicate page numbers or duplicate reading orders within a page are rejected.
Block text is retained exactly, without NFC conversion, trimming, whitespace
folding, or repeated heading prefixes. Tables render their authoritative `rows`
with a tab between cells and a newline between rows; flattened `table.text` is
ignored. All consecutive blocks, including across pages, are joined by exactly
two newlines. Empty blocks are represented too.

Header/footer text remains in this canonical representation so deleting furniture
cannot shift later offsets. It is never included in a chunk, and no chunk range
or overlap crosses a header/footer block. Table, clause, section, and language
changes also separate chunk runs. Empty/whitespace-only content produces no chunk.

Offsets are global, zero-based, end-exclusive **Unicode code-point** indices into
this representation, not byte offsets, PDF text-layer offsets, or JavaScript
UTF-16 indices. Consumers must use this same representation/version when slicing.
The returned `CanonicalDocument.blocks` maps each source block to its exact span.
This is a deterministic view of the parsed document; it does not mutate or rewrite
stored ParseResult JSON.

Every emitted Chunk populates both v1 legacy aliases (`id`, `page`) and enriched
fields (`chunk_id`, `page_start`, `page_end`, `block_ids`, `heading_path`, and
`language`). `page` equals `page_start`. Block IDs are derived without mutating
parser output: `<doc_id>:p<page>:b<reading_order>`. They resolve directly through
the canonical block-span map and are stable for the same parsed document version.
Chunk IDs are UUIDv5 hashes of document ID and the exact start/end offsets, shared
with vector storage through `intellidocs_intelligence.chunk_ids.chunk_id`. Text
changes at the same offsets retain the ID so a vector upsert replaces that span.

`text` is always an exact canonical slice. Page range and block IDs include only
blocks intersecting that slice. A split clause retains its `clause_number` and
heading context on every part; a heading path includes the current heading when
the chunk contains it. Metadata from existing parsed blocks takes precedence;
heading levels support legacy blocks without explicit ancestor paths. The caller
may pass `department`, `doc_date`, and `doc_type` from org config/document metadata.

The Chunk contains the quote, document ID, global offsets, source block IDs, and
page range needed for a chunk-level citation without running the chunker again.
For a multi-page chunk, retain `page_start`/`page_end`; the legacy `page` field
identifies only its first page. Resolving a smaller quote to a particular page or
bounding box requires the canonical source-block map, not a guessed page number.

## Boundary and overlap policy

Small clauses/sections are kept whole. Longer runs prefer paragraph ends that
are also sentence ends, otherwise sentence ends. Sentences can continue across
block boundaries. A conservative offline punctuation detector recognizes full
stops, question/exclamation marks, and danda/CJK terminal punctuation, while
protecting clause prefixes, initials, common abbreviations, and decimal numbers.
It does not claim full linguistic sentence disambiguation. Structural barriers
delimit independent runs even when OCR has omitted terminal punctuation.

Tokens are never used as arbitrary cut positions. An oversized detected sentence
or table remains whole and can exceed the target. Requested overlap rounds up to
whole trailing sentences, so it can exceed the configured overlap. It never
crosses a structural boundary. A window must add new content; an indivisible
single-unit window cannot overlap itself indefinitely. No synthetic text is
prepended to make the size or overlap exact.

The chunker only returns chunks and their citation metadata. Indexing, embeddings,
and vector persistence are not wired into this component.
