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

## Obligation extraction evaluation

The [gold set](extraction/sample.jsonl) is UTF-8 JSONL, one record per document.
Use its ingested `doc_id` and the document's evaluation `language` tag.
An empty `obligations` list labels a document with no obligations.

```jsonl
{"doc_id":"reports-en","language":"en","obligations":[{"action":"File the report","owner":{"department":"finance","role":null},"deadline":"2026-12-05","page":1,"clause_number":"1.1"}]}
```

For each obligation, write the expected `action`, canonical owner department
and role, and normalized calendar deadline in YYYY-MM-DD. Use `null` for an
owner component or deadline when the expected result is unresolved or absent.
Label the source with a one-based `page` and, when available, its
`clause_number`. Use one label per distinct obligation.

The evaluator reports the following metrics overall and per gold document
language, with totals aggregated before computing rates.

- **Obligation precision, recall and F1** use one-to-one action matching within
  each document. Actions are normalized with Unicode NFKC, case folding, and
  whitespace folding. Symmetric character similarity uses Python's
  `SequenceMatcher` with a configurable threshold of 0.85 by default. Matching
  maximizes pair count, preferring higher similarity and using the labeled page
  and clause to break local ties. Rejected drafts are counted separately.
- **Owner and deadline accuracy** use matched pairs. Owner fields must match
  both canonical labels, and deadlines must match the normalized date. A gold
  null requires an unresolved, null prediction for the corresponding field.
- **Citation faithfulness** is the fraction of emitted citations whose literal
  quote occurs inside the specified character span of the same document, with
  the stated page bounds. Spans use zero-based, end-exclusive Unicode offsets
  into canonical parsed text. Uncited predictions are reported separately.
- **Needs review share** is the fraction of all emitted obligations, including
  rejected drafts, with status `needs_review`.

Undefined rates appear as `null` in JSON and `n/a` in the table. A missing
document, source, or prediction fails the run. Provide an explicit
ExtractionResult with an empty obligation list when the extractor emits none.

Run the synthetic English and Hindi example offline:

```sh
uv run --project services/intelligence --no-sync python eval/extraction/evaluate_extraction.py
```

The bundled run scores hand-authored results. It exercises the evaluator's
failure cases without running the verifier. Two of the three emitted citations
are faithful (66.7%):

- In `reports-en`, `file` cites the full report sentence at `[0:48]`. The
  synthetic false positive `extra` (“Send payment”) supplies that same quote
  but cites `[0:7]`, which contains only “Finance”.
- In `notices-hi`, `notice` has a faithful citation at `[0:39]). Its owner and
  deadline are deliberately unresolved, so it is flagged `needs_review` and
  misses both field accuracy checks.
- In `reports-en`, `rejected` (“Retain the audit copy”) is deliberately marked
  rejected with no citations. Its gold action counts as a missed extraction
  and it contributes no citation to the faithfulness denominator.

To score your own snapshots, provide `--gold PATH --predictions PATH --sources PATH`.
Prediction and source files are JSONL containing one v1 ExtractionResult and
one v1 ParseResult per gold document, respectively. The ParseResult supplies
the actual source text and page boundaries for checking citations. See the
[sample predictions](extraction/sample_predictions.jsonl) and
[sample sources](extraction/sample_sources.jsonl).

To run the extractor instead of loading predictions, use
`--gold PATH --existing-index --storage-root PATH` with the configured Qdrant and
LLM environment. Keep the stored ParseResults, index, and org config fixed
across comparisons. Set `--similarity-threshold` explicitly to use another cutoff.
Report JSON includes the effective scoring settings, input hashes, raw counts,
and per-document scores. Existing-index runs also record the model, extraction
limits, and org config fingerprints. Reports default to ignored
`eval/results/extraction_<UTC-date>.json`. Use `--output` to choose a report path
or `--overwrite` to replace an existing report.

## Document relation evaluation

The [sample gold set](relations/sample.jsonl) is UTF-8 JSONL with the newer
and older document IDs in that order. Give each relation its expected
`relation_type` (`supersedes`, `amends`, `withdraws`, `conflicts_with`, or
`references`), `scope` (`whole_document` or `clauses`), and one-based
`source_pages` and `target_pages` containing the evidence. Combine pages for
multiple instances of the same type and scope in a pair. Explicitly label
unrelated pairs with `null` type and scope and empty page lists. Include the
plausible negative pairs for each evaluated newer document. The runner requires
an explicit label for every pair predicted in the run.

```jsonl
{"source_doc_id":"amendment-new","target_doc_id":"amendment-old","relation_type":"amends","scope":"clauses","source_pages":[1],"target_pages":[1]}
{"source_doc_id":"unrelated-new","target_doc_id":"unrelated-old","relation_type":null,"scope":null,"source_pages":[],"target_pages":[]}
```

- **Precision, recall and F1** match on the directed pair, type and scope,
  overall and per relation type. Multiple predictions of the same tuple count
  once for classification. Dismissed relations are excluded.
- **False supersession rate** counts older documents predicted to be fully
  superseded or withdrawn divided by older documents with no gold full
  supersession or withdrawal. It counts each older document once, including
  a partially amended document if it was wrongly flagged in full. Default
  scoring treats proposals as potential decisions. The report also gives the
  rate for confirmed decisions alone. Partial amendments have clause-level
  effects and are scored in the relation-type metrics.
- **Citation faithfulness** checks the literal quote against the canonical
  character span and reported page range in the correct document. It counts
  every emitted citation from both sides. Predictions missing evidence on
  either side are counted separately. **Gold evidence page coverage** measures
  the share of gold relations whose labeled pages on both sides are covered by
  faithful citations from a matching prediction.

Run the bundled synthetic example from the repo root.

```sh
uv run --project services/intelligence --no-sync python -m eval.relations.evaluate_relations
```

The fixture deliberately omits a reference and proposes a false supersession
with one incorrect citation span. Its potential false supersession rate is
25%, its confirmed rate is 0%, and citation faithfulness is 90% (9 of 10).
These scores exercise the evaluator with hand-authored outputs.

For real snapshots, pass `--gold PATH --predictions PATH --sources PATH`.
Predictions are v1 `RelationResult` JSONL with one row per newer document,
including an empty `relations` array when none were found. Sources are v1
`ParseResult` JSONL for both sides. Use `--confirmed-only` to score reviewed
output. To run detection directly, pass `--gold PATH --storage-root PATH`
with the configured index and LLM environment. Keep the index and parsed
sources fixed across comparisons. Reports save raw counts and available input
hashes to ignored `eval/results/relations_<UTC-date>.json`. Undefined rates
are `null` in JSON and `n/a` in the table. Use `--output PATH` or `--overwrite`
to control the report destination.
