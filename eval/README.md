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

## Answer evaluation

[`answers/evaluate_answers.py`](answers/evaluate_answers.py) runs retrieval,
reranking, generation, and verification in process. Its default `--mode both`
generates drafts once per query and scores the same drafts before and after
verification. `--mode baseline` retains every generated claim and never invokes
the verifier or its optional support LLM. The HTTP answer route always verifies.

The gold set is UTF-8 JSONL, one record per query:

```jsonl
{"id":"deadline","query":"When are reports due?","language":"en","expected_answer_points":[{"text":"Reports must be submitted by Friday.","aliases":["Reports are due Friday."]}],"relevant":[{"doc_id":"reports","page":2}],"answerable":true}
{"id":"unknown","query":"Are travel expenses reimbursed?","language":"en","expected_answer_points":[],"relevant":[],"answerable":false}
```

`expected_answer_points` accepts strings or objects with `text` and optional
human-authored `aliases`. `relevant` lists the supporting document IDs and
one-based pages. Answerable questions require nonempty points and sources;
unanswerable questions require both lists to be empty. IDs must be unique.
`language` may be omitted only when `--language` explicitly supplies the answer
language. Optional `filters` use the search filter format and apply in both modes.

Scoring uses gold labels independently of the verifier's status. A claim must
match a gold point or alias after canonical Unicode normalization, whitespace
folding, and case folding. Its citation must also slice back to the actual
retrieved source and identify a relevant document/page. This is a conservative
exact-label evaluation: valid paraphrases without an annotated alias count as
unsupported. It does not provide an automatic semantic judgment. Include
representative human-authored aliases and audit the saved claims for a report.

- **Citation precision**: correct citations divided by attempted citations.
  An unresolved chunk ID or quote still counts as one failed citation attempt.
- **Unsupported-claim rate**: claims without a gold-supported, traceable citation
  divided by generated/displayed claims, separately for baseline and verified.
- **Abstention on unanswerable questions**: unanswerable queries with no output
  claims divided by all unanswerable queries.
- **Answer coverage**: distinct gold points covered by supported output claims
  divided by all expected points across answerable questions. Duplicate claims
  cannot increase coverage. Abstained answerable questions contribute zero.

Rates use aggregate counts, saved alongside per-query outputs. A zero denominator
produces JSON `null` and table `n/a`, rather than a perfect score. Generation
validation failures are recorded per query and yield empty output claims.

Run the bundled synthetic fixture with no model weights or external calls:

```sh
EMBEDDING_BACKEND=fake SPARSE_EMBEDDING_BACKEND=native RERANKER_BACKEND=fake \
  VERIFICATION_SUPPORT_CHECK=false \
  uv run --project services/intelligence --no-sync python eval/answers/evaluate_answers.py \
  --responses eval/answers/sample_responses.json
```

The fixture deliberately contains two unsupported drafts and one missed answer
point. Baseline versus verified scores are respectively: citation precision
`0.50 / 1.00`, unsupported claims `0.50 / 0.00`, abstention on unanswerable
questions `0.00 / 1.00`, and coverage `0.667 / 0.667`. These are fixture checks,
not measurements of real model quality.

For model evaluation, omit `--responses` and configure the existing `LLM_*`,
embedding, retrieval, reranking, and verification settings. Supply `--gold PATH`
and either `--chunks PATH` (temporary in-memory Qdrant) or `--existing-index`
(configured Qdrant collection). The latter requires `STORAGE_DIR` or
`--storage-root PATH` to load original ParseResults. With `--chunks`, use
`--storage-root` explicitly for canonical page mapping; without it only
single-page chunk citations can be resolved. Keep the collection and stored
documents fixed for comparisons. LLM determinism depends on the configured
provider; paired modes share the same draft regardless.

Reports default to ignored `eval/results/answers_<UTC-date>.json`; `--output`
selects another path and `--overwrite` permits replacement. Reports contain
input hashes, model/settings, prompt hashes, a Python random seed, both metric
sets, and per-query outputs for review. No verifier bypass is exposed by HTTP.
