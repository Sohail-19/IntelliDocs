# Evaluation

Evaluation datasets, fixtures, and quality checks for document-intelligence capabilities.

[OCR evaluation](ocr/README.md) reports per-document and ground-truth-language
CER/WER, with raw edit counts and explicit failure coverage. Its generated JSON
reports live in ignored `eval/results/`; sample data stays under ignored `data/`.

[`index_bench.py`](index_bench.py) indexes ParseResult JSON folders and reports
embedding/load times, process peak RSS, chunk counts, and Qdrant collection size.
See [performance results and reproduction](../docs/performance.md).

## Retrieval evaluation

The gold set is UTF-8 JSONL with one query per line. The bundled sample has five
synthetic English and Hindi queries and a matching corpus of chunks.
[Sample gold set](retrieval/sample.jsonl) · [Sample chunks](retrieval/sample_chunks.jsonl)

```jsonl
{"id":"en-shipping","query":"WH17 warehouse shipping deadline","language":"en","query_type":"cross_lingual","targets":[{"doc_id":"shipping-hi","page":3,"clause_number":"4.1"}],"filters":{"department":"logistics"}}
```

| Field | Meaning |
| --- | --- |
| `id` | Unique query ID used in the report. |
| `query`, `language` | Query text and language tag, such as `en` or `hi`. |
| `query_type` | `same_language` when the source targets are in the query language, or `cross_lingual` when a relevant source is in another language. |
| `targets` | One or more distinct relevance targets. Each has a `doc_id` and a one-based `page`, a `clause_number`, or both. If both are present, the chunk must match both. A page can fall anywhere in the chunk's inclusive page range. |
| `filters` | Optional string map of metadata constraints known at query time. Only the filtered run applies it. Set filters independently of the relevance judgments. |

Run the sample with the intelligence environment from the repository root.

```sh
EMBEDDING_BACKEND=fake SPARSE_EMBEDDING_BACKEND=native RERANKER_BACKEND=fake \
  uv run --project services/intelligence --no-sync python eval/retrieval/evaluate_retrieval.py
```

For your own gold set, supply `--gold PATH` and either `--chunks PATH` or
`--existing-index`. The chunks file contains one v1 `Chunk` object per line.
The runner embeds these chunks in a temporary in-memory Qdrant collection.
With `--existing-index`, it reads the configured `QDRANT_URL` and
`QDRANT_COLLECTION`. Use a fixed collection snapshot and match the embedding
and sparse settings used to build it. Select `RERANKER_BACKEND=cross_encoder`
with provisioned weights to measure model reranking instead of the fake
lexical scorer. [Intelligence configuration](../services/intelligence/README.md)

The five runs cover dense, sparse, hybrid, hybrid with reranking, and hybrid
with reranking and filters. They use the same candidate pool size and RRF
constant from retrieval configuration. The evaluator keeps the full ranked
pool for scoring and applies cutoffs in the metrics. Every run uses the same
gold targets. This includes queries without filters and targets excluded by
the supplied filters.

- **Recall@5 and Recall@10** measure the fraction of distinct gold targets
  found within the first five or ten results. Overlapping chunks count once
  per target.
- **MRR** averages the reciprocal rank of the first target match over the
  full returned pool. A query with no match contributes zero.
- **nDCG@10** gives a binary gain at each rank that finds a previously unseen
  target. A chunk can cover several targets for recall and contributes at
  most one gain for nDCG. The ideal ranks up to ten distinct targets first,
  one per rank. [NIST nDCG definition](https://trec.nist.gov/pubs/trec20/papers/WEB.OVERVIEW.pdf)

The table and JSON report give macro averages over queries and counts for
the full set, each query language, and each observed query type. The report
also saves per-query scores, the effective settings for each run, input
hashes for the gold set and local corpus, and the seed. Python, NumPy, and
PyTorch (when used) are seeded with 42 by default. Use the same inputs, model
revisions, and runtime for repeat comparisons.

The default report path is `eval/results/retrieval_<UTC-date>.json`, ignored
by Git. Use `--output PATH` to keep another run or `--overwrite` to replace an
existing report. The sample checks the pipeline on five synthetic queries.
Use representative human-labeled queries and a fixed corpus to report
retrieval quality on real documents.
