# Intelligence Service

Python/FastAPI service for OCR, multilingual retrieval, grounded answers, and obligation extraction.

## Offline PDF parsing

The `app.parsing` package exposes `DocumentParser.parse(Path, languages) ->
ParseResult`. Load the organization's YAML into `OrgConfig`, then use
`parser_from_config(config)` and pass `config.languages` to `parse`.
No language or engine defaults are chosen by the parser factory.

- `ocr_engine: docling` selects Docling for pages with extractable text, with
  Tesseract for image-only pages. Mixed PDFs preserve their original page order.
- `ocr_engine: tesseract` explicitly selects OCR for every page.

Install `uv sync --extra tesseract --extra docling` for both paths. The base
installation does not include PDF, image, language-detection, or model libraries.
The `parsing` extra contains PDF inspection and offline language utilities;
`tesseract` adds PDFium/Pillow and requires the system Tesseract binary and packs.
ISO 639-1 codes are supplied by org config, translated to Tesseract codes, and
checked against the installed packs. Missing packs and unknown engines or ISO
codes produce explicit errors. Page language detection is restricted to the org's
configured candidates; single-language/empty pages use the configured candidate.
Tesseract groups words into lines and recognizes simple aligned table columns.
Complex table/layout classification is provided by Docling for digital pages.
Tables keep cell content in `rows` and emit empty `text`; repeated margin text
and page numbers are retained as `header_footer` blocks. Use
`app.parsing.structure.content_text(page)` to obtain text without that furniture.
Clause prefixes are retained as `clause_number`. Heading blocks expose
`heading_level`, and body blocks retain an ancestor `heading_path` across pages.

Docling requires `DOCLING_ARTIFACTS_PATH` pointing to model artifacts provisioned
before runtime. OCR and remote services are disabled in Docling; Hugging Face and
Transformers run offline. Missing local artifacts fail rather than downloading.
Model provisioning is deliberately outside this parser's runtime behavior.

Docker installs Tesseract and packs from the build argument/environment variable
`TESSERACT_LANGUAGES` (comma-separated, default `eng,hin`). Change it in `.env`
and rebuild when an org needs additional packs. Set `PARSING_EXTRAS=docling,tesseract,formats`
to include Docling in the image and mount local artifacts before selecting it.
The default image includes the Tesseract and formats extras. Installing packs does not select
an organization's runtime languages; only org config does that.

Regenerate the original, non-copyrighted fixture PDFs with:

```sh
uv run python tests/fixtures/generate_pdfs.py
uv run ruff check .
uv run mypy
uv run pytest
```

Tests mock Docling model conversion and run real Tesseract when installed. The
Docling adapter is tested with layout objects without downloading models. Parser
tests block Python network connections.

## Ingestion

`POST /v1/ingest` accepts multipart `file` and optional `org_id`. Supported formats
are PDF, PNG, JPG/JPEG, DOCX, EML, and MSG. Install `uv sync --extra tesseract
--extra formats` for these readers, plus the configured system OCR language packs.
Set `STORAGE_DIR` to a writable local directory and `ORG_CONFIG_DIR` to the
repository's `config/orgs` directory. Supply `org_id`, or configure
`DEFAULT_ORG_ID` to a config filename stem. Org YAML is validated with `OrgConfig`.

`MAX_UPLOAD_BYTES` defaults to 20 MiB; it bounds the file and the combined upload
plus decoded attachments. DOCX expanded archive size is bounded too. Unsupported
types/signatures return 415, oversize uploads return 413, malformed documents
return 422, and missing parser dependencies return 503. No supplied MIME header
is trusted in place of file type validation.

Each document gets a UUID directory with `source.<extension>` and
`parse_result.json`. A failed parse removes files created by that ingestion.
Supported email attachments are recursively parsed and stored with
`parent_document_id`; unsupported attachment types produce warnings. Recursion
is bounded to five attachment levels and 100 documents per request. HTML email
is reduced to text without fetching images or other remote resources.

DOCX paragraphs/tables and email bodies use logical pages with an explicit
pagination warning. Image uploads are converted locally to PDF before parsing.
Storage implements a small interface so a remote backend can later stage files
locally for the same parsers. The API response is returned only after JSON has
been written; the existing contract retains status 202.

Generate the original additional upload fixtures with
`uv run python tests/fixtures/generate_uploads.py`. Tests decode real DOCX, EML,
and MSG files, mock the PDF model boundary, and run real image OCR when the
system binary is available.

## Embedding interface and index contract

`intellidocs_intelligence.embeddings.Embedder.embed(texts)` returns one object per
input, in order: `{"dense": [float, ...], "sparse": {"indices": [int, ...],
"values": [float, ...]}}`. Sparse indices and weights are aligned and sorted.
An empty batch returns an empty list without model initialization.

`EMBEDDING_BACKEND=fake` (default) uses deterministic SHA-256 token hashing and
normalized 16-dimensional dense vectors. It is a test double, not semantic
retrieval; identical inputs are stable across processes. Empty text has a zero
dense vector and empty sparse vector.

`EMBEDDING_BACKEND=bge_m3` uses the optional `bge-m3` extra:

```sh
uv sync --extra bge-m3
```

`EMBEDDING_MODEL` defaults to `BAAI/bge-m3` and may instead be a local model
directory. `EMBEDDING_DEVICE` defaults to `cpu` (or select e.g. `cuda:0`). Imports
and weights are loaded only on the first nonempty `embed` call, once per adapter.
The adapter asks FlagEmbedding for dense vectors and lexical weights, not
ColBERT vectors, and converts results to ordinary Python floats/integers.

Provision all weights/tokenizer files before runtime. Hub model identifiers are
resolved with `local_files_only=True`; Hugging Face/Transformers offline mode and
`trust_remote_code=False` remain enforced. Missing caches or optional libraries
produce an explicit error. Tests use a mocked FlagEmbedding boundary; they do
not download BGE-M3 or require GPU libraries. Include `bge-m3` in Docker's
`PARSING_EXTRAS` build argument only when that image needs it, and mount the local
model directory/configure `EMBEDDING_MODEL` before selecting the backend.

`POST /v1/index` accepts `{"doc_id": "<stored-document-id>"}`. It loads the stored
ParseResult, builds structural chunks, embeds batches of `EMBEDDING_BATCH_SIZE`
(required; the environment template supplies 32), and waits for Qdrant writes.
It returns IndexResult with the acknowledged chunk count and model identity.
Missing documents return 404; invalid IDs or changed original files return 400;
backend failures return 503 and can be retried with the same document ID.

Each document stores a source SHA-256 and an atomic `index_cache.json` beside
its ParseResult. The cache includes vectors and a fingerprint of source content,
chunk texts/settings, and embedder identity. An unchanged document skips model
calls, including after a process restart or failed Qdrant write. Metadata-only
updates refresh payloads using cached vectors. Changing content, chunk boundaries,
or model identity re-embeds. If replacing model weights in place, change the model
identifier or clear the cache. A per-document file lock serializes local workers.
After upserting current chunks, obsolete spans are removed. Multi-batch writes
are not atomic: a failure can leave partial points until a retry completes.

Multipart ingest accepts `index=true` to index the uploaded document and email
attachments immediately. The default remains parse-only. Success includes
`index_results`; an indexing failure returns 503 with the retained document ID,
parse result, and `index_error`. Retry the retained IDs through `/v1/index`.
`department`, `doc_type`, and `doc_date` can be supplied as multipart fields or
as `upload_defaults` in the org config. Request values win, absent values remain
absent, and dates use YYYY-MM-DD. Department/type must belong to their configured
org lists when those lists are nonempty. Metadata is propagated to attachments.

The optional `sentence-transformers` extra adds offline dense embeddings for
smaller multilingual models. `SPARSE_EMBEDDING_BACKEND=auto` pairs these with
offline BM25-style sparse vectors and retains BGE-M3's native learned weights.
Use `embedder_from_env()` for the configured pair and `embed_queries()` for query
vectors. E5 needs `EMBEDDING_QUERY_PROMPT="query: "` alongside its document prompt.
Sparse policy/parameters are included in embedding-cache fingerprints. See
[performance results and model switching](../../docs/performance.md) for measured
E5-small memory/load costs, sparse settings, required prompts, and benchmark commands.

## Structural chunks

[`app/chunking`](app/chunking/README.md) turns parsed blocks into clause/section
chunks, isolates tables and page furniture, and records exact canonical text
offsets and source block IDs. Configure `CHUNK_TARGET_TOKENS`,
`CHUNK_OVERLAP_TOKENS`, and `CHUNK_TOKEN_COUNT_METHOD`, then use
`Chunker(ChunkingConfig.from_env()).chunk(parsed_document)`. The module documents
the canonical-text format, sentence/overlap policy, and citation reconstruction.

## Vector storage

[`app/vector_store`](app/vector_store/README.md) provides Qdrant persistence with
named dense/sparse vectors, payload indexes, span-derived UUID identities, and
document deletion. Set `QDRANT_URL` and `QDRANT_COLLECTION` for Compose, or inject
an in-memory `QdrantClient` for tests. Dense dimensions are inferred from the
embedder. [`app/indexing.py`](app/indexing.py) orchestrates chunking, embedding,
cache persistence, and vector writes. The internal retrieval module provides
hybrid, dense-only, and sparse-only search over indexed chunks.
[Retriever configuration and usage](app/retrieval/README.md)

`POST /v1/search` retrieves the configured candidate pool, reranks it, and
returns up to the requested `top_k` results with citations copied from each
chunk. Include `debug: true` in the JSON body to get stage timings and backend
names. [Search contract](../../contracts/README.md)

Call `create_reranker().rerank(query, candidates, top_n)` on a retriever's
candidate pool. Rerankers keep the original retrieval score and source. Scoring
backends add `rerank_score` and keep retrieval order for ties. The no-op backend
returns the first `top_n` candidates with their existing scores.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `RERANKER_BACKEND` | `none` | Select `none`, `fake`, or `cross_encoder`. |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | Single-logit cross-encoder model ID or local directory. |
| `RERANKER_DEVICE` | `cpu` | PyTorch device. |
| `RERANKER_BATCH_SIZE` | `8` | Maximum query and chunk pairs per inference batch. |
| `RERANKER_MAX_LENGTH` | `512` | Token limit for the pair, including special tokens. |

Install the optional backend with `uv sync --extra reranker`. For Docker, include
`reranker` in `PARSING_EXTRAS`. Provision the model and tokenizer locally before
running inference. The adapter loads them on the first scoring call and reuses
them on subsequent calls. It uses the tokenizer's length limit and trims tokens
from the longer sequence as needed. Scores are the model's raw relevance logits.
[Default model card](https://huggingface.co/BAAI/bge-reranker-v2-m3)

## Grounded answers

`POST /v1/answer` accepts `query`, `language`, `top_k`, and optional `filters`
from the v1 AnswerRequest contract. It retrieves the full candidate pool,
reranks to `top_k`, generates draft claims in the requested language, and
verifies their quotes against retrieved chunks and stored ParseResults.

```sh
curl http://localhost:8000/v1/answer \
  -H 'content-type: application/json' \
  -d '{"query":"When is the report due?","language":"en","top_k":5}'
```

The returned Answer includes only verified claims and their source citations.
Missing source files, unsupported drafts, empty retrieval, and exhausted
generation-validation retries produce an abstention when no verified claim
remains. Invalid requests/filters return 422; unavailable pipeline dependencies
return 503. HTTP does not accept a verifier-off flag.

Set `STORAGE_DIR` to the directory used during ingestion and indexing, and
configure the existing `LLM_*`, retrieval, reranking, and verification settings.
[Generation settings](app/generation/README.md) ·
[Citation and optional support verification](app/verification/README.md) ·
[Paired answer evaluation](../../eval/README.md#answer-evaluation)
