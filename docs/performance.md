# Indexing performance and free-host budgets

The measured multilingual E5-small and BGE-M3 indexers exceeded a 512 MB worker
budget even on tiny documents. A fake-embedder smoke test fits that memory
budget, but it cannot establish whether semantic indexing fits. These are local
measurements, not deployment capacity or retrieval-quality results.

The neural model in the original reported run was exactly
`intfloat/multilingual-e5-small`, using `EMBEDDING_BACKEND=sentence_transformers`
(`SentenceTransformerEmbedder`), CPU, and `EMBEDDING_DOCUMENT_PROMPT="passage: "`.
The JSON identity records the provisioned local directory for revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`; that directory contains the
multilingual E5-small model, not `intfloat/e5-small` or `intfloat/e5-small-v2`.
No model substitution is needed. Separate fake smoke tests used
`EMBEDDING_BACKEND=fake` and `fake-sha256-v1`; the benchmark's unset-env default
remains the fake backend, so a neural benchmark must select its backend explicitly.

The initial 2026-11-15 measurements and the BGE-M3 rerun on 2026-11-16 used
Python 3.11.17, macOS 27.2.1 arm64 (10 logical CPUs), qdrant-client 1.12.2,
PyTorch 2.15.0, and Sentence Transformers 6.3.0. BGE-M3 additionally used
FlagEmbedding 1.4.2 through the existing optional `bge-m3` extra.
The real Qdrant server was v1.12.6 in a disposable Docker container limited to
one CPU and 1 GiB. The Python indexer ran on the host without a resource limit.

The corpus was 20 separately ingested copies of the repository's original
`services/intelligence/tests/fixtures/upload.docx`: a heading, short English
numbered clause, and two-row table. Each produced three chunks, for 60 total.
`CHUNK_TARGET_TOKENS=400`, `CHUNK_OVERLAP_TOKENS=50`,
`CHUNK_TOKEN_COUNT_METHOD=unicode_words`, and `EMBEDDING_BATCH_SIZE=32` were used.
Batching follows the service's per-document policy: these files made 20 batches
of three, so this does **not** measure the peak for a full batch of 32 long chunks.
Department/type came explicitly from the example org's lists; the date was
explicitly supplied. No metadata was inferred from the file.

The side-by-side neural-model comparison below uses three fresh-process runs
per model. Load time and ms/chunk are medians; peak RSS is the maximum of the
three OS high-water readings. The E5 row retains the original 2026-11-15 runs;
BGE-M3 was measured on 2026-11-16 using the **same saved 20 documents** and the
same chunk/batch settings, CPU device, and Qdrant version/limits. Each of all six
reports has the same corpus fingerprint and 60 acknowledged chunks/points.
OS file caches were not flushed, so these load times do not establish which
model has a faster cold boot on another host.

| Exact model | Backend | Median load s | Median ms/chunk | Max peak RSS MiB | Model files MiB |
| --- | --- | ---: | ---: | ---: | ---: |
| `intfloat/multilingual-e5-small` | `sentence_transformers` | 3.6268 | 4.1756 | 991.20 | 486.26 |
| `BAAI/bge-m3` | `bge_m3` | 2.9059 | 41.4091 | 2,094.14 | 2,205.38 |

Model disk size means the sum of logical sizes of the provisioned weights,
tokenizer, and config files, including ancillary config copies selected during
provisioning, excluding `.cache` download metadata and Python dependencies.
It is separate from Qdrant collection/snapshot size. The exact totals are
509,879,283 bytes for E5-small and 2,312,505,647 bytes for BGE-M3 (MiB = 2^20
bytes). Checkpoints alone occupy 470,641,600 bytes (`model.safetensors`) and
2,271,145,830 bytes (`pytorch_model.bin`), respectively. Model files and all raw
reports remain gitignored.

BGE-M3 used revision `5617a9f61b028005a4858fdac845db406aefb181`, provisioned before
timing, with `EMBEDDING_DEVICE=cpu` and the adapter's existing `use_fp16=False`.
It produced 1,024-dimensional dense vectors and nonempty learned sparse vectors;
E5 produced 384-dimensional dense vectors with empty sparse vectors. This is a
comparison of the configured indexing paths, not retrieval quality or identical
embedding functionality. BGE's three runs recorded load times 4.1097, 2.7943,
and 2.9059 s; embedding times 80.9956, 40.8502, and 41.4091 ms/chunk; and peak RSS
2,075.11, 2,092.08, and 2,094.14 MiB. Each collection snapshot was 822,784 bytes.
Reports are `eval/results/index_bge_m3_server_1.json` through `_3.json`;
`index_model_disk_2026-11-16.json` records the per-model file sizes and
`index_model_comparison_2026-11-16.json` records the verified aggregates.

The original smoke-test results, including the fake backend, are retained here:

| Embedder / Qdrant mode | Documents / chunks | Embedding ms/chunk | Model load s | Peak indexer RSS MiB | Collection size bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fake, SDK local SQLite | 20 / 60 | 0.0059 | <0.000001 | 83.16 | 102,400 |
| Fake, real server | 20 / 60 | 0.0064 | <0.000001 | 84.11 | 276,992 |
| Multilingual E5-small, CPU, real server | 20 / 60 | 4.1756 | 24.2108 | 991.20 | 477,696 |

All runs acknowledged 60 points. Local indexing, excluding model loading and
size measurement, took 0.0305 s for the fake SDK run and 0.1116 s for the fake
server run. The fake model has 16 dense dimensions and hashed sparse vectors;
E5-small has 384 dense dimensions and this adapter emits **empty sparse vectors**.
Its numbers are not evidence for hybrid retrieval quality.

The smoke-test table shows the first run of each configuration. Two more
fresh-process E5 runs on the same corpus produced 4.8395 and 2.2670 ms/chunk, model load times
of 3.6268 and 2.6592 s, and peak RSS of 931.80 and 975.83 MiB. Across all three,
median embedding time was 4.1756 ms/chunk, median load time 3.6268 s, and maximum
peak RSS 991.20 MiB. Each wrote 60 points with a 477,696-byte snapshot. The wide
startup range illustrates the effect of library/file caches. Repeated reports
are `eval/results/index_e5_small_server_2.json` and `_3.json`.

E5 weights were provisioned before the run from
`intfloat/multilingual-e5-small` revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`. The safetensors file was 470,641,600
bytes; selected model/tokenizer/config files totaled 509,879,283 bytes. Downloads
are excluded from load time and embedding time. Raw JSON reports are local,
gitignored files: `eval/results/index_fake_baseline.json`,
`index_fake_server.json`, and `index_e5_small_server.json`. Their common corpus
fingerprint is `50fab325ea534dc7c28ce130ebee1947731235d1f29a5fb8e6b58d1a2cb1e7ad`.
Fresh ingests generate new IDs and thus a new manifest fingerprint; reuse the
same parsed folder when comparing models.

Embedding time is the sum of `embed` wall times divided by the number of embedded
chunks. Explicit `load()` timing includes lazy library imports and loading
already-provisioned weights, and is excluded from embedding time. The first
forward pass remains in embedding time. Run each benchmark in a fresh process;
OS file caches and CPU scheduling can still change load/encode times.

Peak RSS is the OS lifetime high-water mark of the Python benchmark process,
including imports and embedding allocations. Darwin's byte and Linux's KiB
`ru_maxrss` units are normalized to bytes. It excludes the remote Qdrant process,
core, Postgres, FastAPI serving overhead, OCR, GPU memory, and concurrent workers.
SDK-local mode includes local Qdrant's Python allocations. This benchmark uses
the production chunker, embedder, and vector store; it deliberately bypasses
the service's existing embedding cache, original-file verification, and disk
cache writes. A warm re-index that reuses embeddings answers a different question.

Collection size is labeled in JSON: `local_sqlite_files` sums logical file sizes
inside the dedicated local collection directory after closing the client;
`server_snapshot` measures a newly created collection snapshot and deletes that
snapshot afterward. Snapshot size is **not** server RSS, allocated filesystem
usage, total volume usage, or an estimate for a million vectors. WAL allocation,
other collections, snapshots retained elsewhere, and compaction headroom need
additional disk. SDK-local mode does not build payload indexes or the server's
production storage/index structures, so its size cannot predict server capacity.
A separate `du -sk` check of the running server's collection directories measured
764 KiB allocated for the fake collection and 960 KiB for each E5 collection;
this includes files omitted or represented differently in a snapshot. After all
four 2026-11-15 server runs, the container cgroup high-water memory counter was
288,350,208 bytes (about 275 MiB), including page cache. That counter is not RSS
and is not part of the indexer's RSS column. The workload is too small to
extrapolate useful per-document disk or server memory capacity.

The free-tier implications are:

- A [Render free web service](https://render.com/pricing) advertises 512 MB RAM.
  The measured E5 worker already peaked at 991.20 MiB; the full stack will not
  fit that worker budget. Even a 1 GiB indexer leaves little headroom at this
  observed peak, before longer inputs, OCR or concurrent work. macOS RSS is not
  an exact Linux container requirement: rerun under the target cgroup limit.
- BGE-M3's measured peak was 2,094.14 MiB (about 2.05 GiB), already above a
  2 GiB worker budget before serving overhead or other services. Its provisioned
  model files also need about 2.31 GB of disk separately from Qdrant and uploads.
  These are measured values for the tiny corpus, not guarantees for full batches
  or larger documents. Neither measured neural backend fits a 512 MB worker.
- [Qdrant Cloud's free cluster](https://qdrant.tech/documentation/cloud/create-cluster/)
  currently provides 1 GB RAM, 0.5 vCPU, 4 GB disk, and one node, for prototypes.
  That budget belongs to Qdrant; it does not provide RAM for the embedding model.
  Idle free clusters suspend after one week and can be deleted after four weeks.
  The local one-CPU server run above is not a test of that managed free tier.
- [Render free services](https://render.com/docs/free) lose local files on
  restart, redeploy, or idle spin-down and cannot attach persistent disks. Free
  Postgres expires after 30 days. The current local uploads/ParseResults/cache
  storage therefore cannot provide durable service on that setup as it stands.
  Changing only the embedding model does not solve persistence or the other
  services' budgets. These limits were checked on 2026-11-15 and may change.

For a scale estimate, dense FP32 data alone costs `chunks × dimensions × 4`
bytes: 100,000 vectors need about 390.6 MiB at BGE-M3's 1,024 dimensions versus
146.5 MiB at E5-small's 384 dimensions. This calculated lower bound excludes
sparse vectors, text/metadata payloads, indexes, WAL and database overhead.
The dimensions and model capabilities are documented in the authors'
[BGE-M3](https://huggingface.co/BAAI/bge-m3) and
[E5-small](https://huggingface.co/intfloat/multilingual-e5-small) model cards.

To reproduce a baseline, use the intelligence environment from the repository
root. The following creates ignored input files; it refuses an existing corpus
directory so rerunning does not quietly double the sample:

```sh
uv sync --project services/intelligence --all-groups
uv run --project services/intelligence --no-sync python - <<'PY'
from pathlib import Path
from fastapi import UploadFile
from app.ingestion import IngestionService, load_org
from app.storage import LocalStorage
from intellidocs_intelligence.models import UploadMetadata

root = Path("data/index-bench/parsed")
root.mkdir(parents=True, exist_ok=False)
config = load_org("example")
metadata = UploadMetadata(department=config.departments[0],
    doc_type=config.document_types[0], doc_date="2026-11-15")
service = IngestionService(LocalStorage(root))
for n in range(20):
    with Path("services/intelligence/tests/fixtures/upload.docx").open("rb") as file:
        service.ingest(UploadFile(file=file, filename=f"fixture-{n:02}.docx"), config, metadata)
PY

export CHUNK_TARGET_TOKENS=400 CHUNK_OVERLAP_TOKENS=50
export CHUNK_TOKEN_COUNT_METHOD=unicode_words EMBEDDING_BATCH_SIZE=32
EMBEDDING_BACKEND=fake uv run --project services/intelligence --no-sync \
  python eval/index_bench.py --documents data/index-bench/parsed
```

The CLI also accepts a flat folder of standalone ParseResult JSON exports.
It recurses through JSON files, skipping only `index_cache.json`. Use a dedicated
input folder, without unrelated JSON. Nested email attachments are indexed once
per document ID, including when their own stored JSON also occurs in the folder.
Conflicting content for one ID is an error. Metadata is copied from each stored
Document; the benchmark neither invents it nor reapplies today's org defaults.
Partial runs retain an error list and return exit status 1. Generated reports
default to `eval/results/index_<UTC-timestamp>_<run-id>.json`; local Qdrant data
also stays under ignored `eval/results/`. `--output` chooses a report location;
`--overwrite` only replaces a report, never an existing collection.

For a real server, add `--qdrant-url http://localhost:6333` (or the explicitly
chosen server URL). Optional authentication comes from `QDRANT_API_KEY` and is
never written into the report. Each run uses a new collection unless
`--collection NAME` is supplied; existing names are rejected. The collection is
retained for inspection, so delete only that benchmark collection afterward.
Provision models before running; the benchmark never downloads them at runtime.

To use the smaller multilingual model, provision its optional backend and
immutable weights once. Both models are MIT licensed. For the revision measured
above, provisioning with network access is:

```sh
uv sync --project services/intelligence --all-groups --extra sentence-transformers
uv run --project services/intelligence --no-sync python - <<'PY'
from huggingface_hub import snapshot_download
revision = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
snapshot_download("intfloat/multilingual-e5-small", revision=revision,
    local_dir=f"data/index-bench/models/{revision}",
    allow_patterns=["*.json", "*.safetensors", "sentencepiece.bpe.model", "vocab.txt"])
PY
```

After provisioning, selection uses environment variables only, in both the
benchmark and the intelligence service:

```sh
export EMBEDDING_BACKEND=sentence_transformers
export EMBEDDING_MODEL="$PWD/data/index-bench/models/614241f622f53c4eeff9890bdc4f31cfecc418b3"
export EMBEDDING_DEVICE=cpu
export EMBEDDING_DOCUMENT_PROMPT="passage: "
export QDRANT_COLLECTION=chunks_e5_small_v1
uv run --project services/intelligence --no-sync python eval/index_bench.py \
  --documents data/index-bench/parsed --qdrant-url http://localhost:6333
```

Use a new collection and re-index every document when changing model or prompt;
the service's cache fingerprint includes both. Restart the intelligence process
to apply environment changes. Do not mix embedding spaces even when dimensions
happen to match. For Docker, include `sentence-transformers` in `PARSING_EXTRAS`
and rebuild once, and provision/mount the model directory inside the container
before selecting its container path in `EMBEDDING_MODEL`. Environment selection
does not install dependencies or mount files. Both the existing BGE-M3 backend
and the new backend load only local weights and disable remote model code.

E5 requires the `passage: ` prefix for indexed documents and `query: ` for future
queries, including non-English text. It has a 512-token input limit; the tokenizer
can truncate longer inputs. The chunker's configurable counting method is not
necessarily the model tokenizer, especially for multilingual text. Validate
quality on representative long English/Hindi inputs before using this model;
the tiny English fixture is a memory/load baseline only. Dense-only E5 supplies
no learned sparse weights. See the author's
[usage and limitations](https://huggingface.co/intfloat/multilingual-e5-small).
No search behavior, quantization, model compression,
or other optimization is introduced here.

To reproduce the BGE-M3 comparison using the same saved parsed folder and the
chunk/batch settings above, provision its pinned weights once with network
access, then run the benchmark offline against your test Qdrant server:

```sh
uv sync --project services/intelligence --all-groups --extra sentence-transformers --extra bge-m3 --locked
uv run --project services/intelligence --no-sync python - <<'PY'
from huggingface_hub import snapshot_download
revision = "5617a9f61b028005a4858fdac845db406aefb181"
snapshot_download("BAAI/bge-m3", revision=revision,
    local_dir=f"data/index-bench/models/bge-m3/{revision}",
    allow_patterns=["*.json", "pytorch_model.bin", "sparse_linear.pt",
                    "colbert_linear.pt", "sentencepiece.bpe.model"])
PY

export EMBEDDING_BACKEND=bge_m3 EMBEDDING_DEVICE=cpu
export EMBEDDING_MODEL="$PWD/data/index-bench/models/bge-m3/5617a9f61b028005a4858fdac845db406aefb181"
export EMBEDDING_DOCUMENT_PROMPT=""
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
for repeat in 1 2 3; do
  uv run --project services/intelligence --no-sync python eval/index_bench.py \
    --documents data/index-bench/parsed --qdrant-url http://localhost:6333 || break
done
```

The measured rerun used a separate temporary server on `127.0.0.1:56333`; the
example command uses the usual local Qdrant port. Use a fresh benchmark collection
per run, as the CLI does by default. It retains those collections for inspection.

Check the benchmark without loading neural models or accessing a network:

```sh
services/intelligence/.venv/bin/python -m pytest eval/test_index_bench.py
services/intelligence/.venv/bin/ruff check --config services/intelligence/pyproject.toml eval/index_bench.py eval/test_index_bench.py
MYPYPATH=services/intelligence/src:services/intelligence \
  services/intelligence/.venv/bin/mypy --strict eval/index_bench.py
```
