# Evaluation

The [combined harness](../eval/run_all.py) runs OCR, retrieval, answers, extraction, relations, and summaries in that order. It runs parsers and retrieval against fixed inputs, measures what the verifier does to the same answer drafts, and scores saved predictions for the remaining stages. The [latest combined run](#latest-combined-results) describes the bundled fixtures and configuration.

## Reproduce the run

Install the Intelligence development environment from [Contributing](../CONTRIBUTING.md#local-checks) and make Tesseract with its `eng` language pack available. Run this from the repository root.

```sh
services/intelligence/.venv/bin/python eval/run_all.py
```

The [suite configuration](../eval/gold/config.json) fixes the gold files, seed, embedding and reranker settings, LLM provider and model, thresholds, and ablations. Pass `--config PATH` for another configuration or `--output-dir PATH` for an independent run. The default output is ignored by Git under `eval/results/<UTC-date>/`. Each run writes `report.json`, `report.md`, `metrics.csv`, `ablations.csv`, and `latency.csv`. The JSON records the exact commit, the dirty-tree flag, input hashes, full config, and effective settings. See the [runner instructions](../eval/README.md#combined-gold-set-evaluation) for live model and index options.

## Bundled gold sets

| Stage | Gold inputs | Size and what is annotated |
| --- | --- | --- |
| OCR | [Manifest and transcripts](../eval/gold/ocr/) | 2 English documents with reference page text. Live Tesseract processing. |
| Retrieval | [Queries](../eval/gold/retrieval.jsonl), [chunk snapshot](../eval/retrieval/sample_chunks.jsonl) | 5 queries, with 3 English and 2 Hindi. There are 3 same-language and 2 cross-lingual queries. Labels identify a document plus a page or clause. |
| Answer | [Queries](../eval/gold/answer.jsonl), [fixture drafts](../eval/answers/sample_responses.json) | 4 queries, including 1 unanswerable and 3 expected points overall. Labels include expected text or aliases and relevant document pages. |
| Extraction | [Obligations](../eval/gold/extraction.jsonl) | 2 documents and 3 gold obligations across English and Hindi. Labels cover action, owner, deadline, page, and clause when available. |
| Relations | [Document pairs](../eval/gold/relations.jsonl) | 6 pair records. Labels describe the expected relation type and scope or absence of a relation, with evidence pages where applicable. |
| Summary | [Audience requests](../eval/gold/summary.jsonl) | 2 requests, including 1 irrelevant request and 1 expected point. Labels identify the document, audience, language, and expected output or abstention. |

The standalone public-subset reports cover 3 CUAD contracts with 5 retrieval queries and 3 SROIE receipts with 10 retrieval queries. The CUAD extraction run is a cue-based contract-span proxy. The SROIE OCR references follow annotation line order, and field retrieval uses annotated text. SROIE has no obligation labels. FUNSD has download guidance and no recorded run. See [public dataset methods and limits](../eval/README.md#public-dataset-evaluation) and [data sources and licenses](../data/README.md).

The [timing set](../eval/timing/questions.jsonl) has 12 questions across both pilot organizations. The timing report at `eval/results/timing/fixture-2026-11-20/report.json` contains fixture API measurements and no manual measurements. A manual-to-system ratio remains unavailable. [Timing protocol](../eval/timing/README.md)

## Metrics

A gold label specifies the expected answer, span, obligation, relation, or summary outcome independently of the system result. A citation is faithful when the quote traces back to the cited document, page, and character span. Relevance and support are scored separately.

| Stage | Metric | How it is computed |
| --- | --- | --- |
| OCR | Character error rate (CER) and word error rate (WER) | Levenshtein edits divided by reference Unicode code points or whitespace-delimited words after normalization. Overall and per-language results pool edits and reference lengths. |
| Retrieval | Recall@5, Recall@10 | Fraction of distinct gold document/page/clause targets covered within the cutoff. Multiple chunks covering the same target count once. |
| Retrieval | Mean reciprocal rank (MRR) | Mean of one divided by the first relevant result's rank in the returned pool. No match contributes zero. |
| Retrieval | nDCG@10 | Rank-discounted gain through rank 10 divided by the ideal gain. A rank that introduces an unseen target gives one binary gain. Retrieval metrics are averaged over queries overall and by language and same-language or cross-lingual status. |
| Answer | Citation faithfulness and citation precision | Faithfulness is the share of attempted citations that trace to the source. Precision also requires the claim to match a gold point or alias and cite a relevant document and page. |
| Answer | Unsupported-claim rate | Share of claims with no citation satisfying the precision criterion. The paired comparison scores the same drafts before and after verification. |
| Answer | Abstention on unanswerable queries | Share of unanswerable queries that yield no claims. |
| Answer | Answer coverage | Share of expected points matched by a supported claim. A correct paraphrase without a matching alias can be missed. |
| Extraction | Precision, recall, F1 | One-to-one matching by normalized action similarity within each document, using the configured threshold. Precision divides matches by non-rejected predictions, recall by gold obligations, and F1 is their harmonic mean. Counts are pooled overall and by language. |
| Extraction | Owner and deadline accuracy | Among matched obligations, the share with the correct resolved department and role, or the correct normalized deadline. Each also checks whether resolution status matches the label. |
| Extraction | Citation faithfulness and needs-review rate | Faithfulness checks every emitted citation against the exact canonical source span and page. The standalone evaluator also reports the share of all emissions marked `needs_review`, including rejected outputs in the denominator. |
| Relations | Precision, recall, F1 | Exact match on the ordered document pair, relation type, and scope. The combined report gives overall and per-type F1. The standalone evaluator also reports precision and recall. |
| Relations | False-supersession rate | Share of target documents with no labeled full supersession or withdrawal that would be marked fully stale by the predictions. The confirmed rate uses only confirmed predictions. |
| Relations | Citation faithfulness and gold evidence page coverage | Faithfulness checks citations on both documents. The standalone evidence coverage metric counts gold relations for which a matching prediction supplies faithful citations covering all labeled pages on both sides. |
| Summary | Citation faithfulness, point coverage, and abstention on irrelevant requests | Faithfulness checks source spans. Coverage is the share of expected points or aliases matched with faithful evidence. Abstention is the share of irrelevant requests marked `not_relevant`. |
| Latency | p50 and p95 | Median and 95th percentile of measured operation durations in milliseconds, using linear interpolation. The suite excludes index construction. The bundled extraction, relation, and summary timings measure snapshot scoring. |

The [classifier evaluator](../eval/classification/README.md) reports held-out label accuracy and macro-F1, with accuracy and one-vs-rest F1 by type and accuracy and macro-F1 by language. Accuracy is the fraction correctly labeled, with abstentions counted as misses. Macro-F1 gives each included label equal weight. The [index benchmark](performance.md) measures model load time, embedding time per chunk, peak process RSS, and model and collection disk footprint. The [timing pilot](../eval/timing/README.md) reports median and p95 for manual and API attempts. Its ratios compare only questions answered correctly by both methods once measurement and review are complete.

## Latest combined results

Generated from `eval/results/2026-11-20/report.json` at 2026-11-20 UTC. Commit `78686fef25c6e259d1ec8298ebb1678b888a7ff4`, dirty working tree, seed `42`. The run used live Tesseract OCR and in-memory Qdrant with `fake-sha256-v1` embeddings and the `lexical-term-overlap` reranker. Answers replayed `fixture-claims.v1` drafts. Extraction, relations, and summaries scored saved predictions.

The tables round the report's `observed_value` fields for inspection. **Low sample** means fewer than 30 items. The original report withholds the publishable value for those rows. Treat the observations as fixture checks. The JSON and CSV retain every subgroup, sample unit, latency, and ablation.

### OCR, extraction, relations, and summaries

| Stage | Metric | Sample | Observed | Status |
| --- | --- | --- | ---: | --- |
| OCR | CER | 2 documents | 0.0% | low sample |
| OCR | WER | 2 documents | 0.0% | low sample |
| Extraction | Precision | 3 predicted obligations | 66.7% | low sample |
| Extraction | Recall | 3 gold obligations | 66.7% | low sample |
| Extraction | F1 | 4 distinct predicted or gold obligations | 66.7% | low sample |
| Extraction | Owner accuracy | 2 matched obligations | 50.0% | low sample |
| Extraction | Deadline accuracy | 2 matched obligations | 50.0% | low sample |
| Extraction | Citation faithfulness | 3 citations | 66.7% | low sample |
| Relations | F1 | 6 distinct predicted or gold relations | 80.0% | low sample |
| Relations | False-supersession rate | 4 eligible target documents | 25.0% | low sample |
| Relations | Confirmed false-supersession rate | 4 eligible target documents | 0.0% | low sample |
| Relations | Citation faithfulness | 10 citations | 90.0% | low sample |
| Summary | Citation faithfulness | 1 citations | 100.0% | low sample |
| Summary | Point coverage | 1 expected points | 100.0% | low sample |
| Summary | Abstention on irrelevant requests | 1 irrelevant requests | 100.0% | low sample |

The extraction snapshot includes a deliberately incorrect citation span. The relation snapshot omits a reference and includes a proposed false supersession. These cases exercise the scorer. [Extraction fixture](../eval/README.md#obligation-extraction-evaluation) · [Relation fixture](../eval/README.md#document-relation-evaluation)

### Retrieval by language and query type

| Group | Queries | Recall@5 | Recall@10 | MRR | nDCG@10 | Status |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Overall | 5 | 100.0% | 100.0% | 0.900 | 0.926 | low sample |
| English | 3 | 100.0% | 100.0% | 1.000 | 1.000 | low sample |
| Hindi | 2 | 100.0% | 100.0% | 0.750 | 0.815 | low sample |
| Same-language | 3 | 100.0% | 100.0% | 1.000 | 1.000 | low sample |
| Cross-lingual | 2 | 100.0% | 100.0% | 0.750 | 0.815 | low sample |

### Paired answer verification

| Metric | Before | Before sample | After | After sample | Status |
| --- | ---: | --- | ---: | --- | --- |
| Citation faithfulness | 50.0% | 4 attempted citations | 100.0% | 2 attempted citations | low sample |
| Citation precision | 50.0% | 4 attempted citations | 100.0% | 2 attempted citations | low sample |
| Unsupported-claim rate | 50.0% | 4 claims | 0.0% | 2 claims | low sample |
| Abstention on unanswerable queries | 0.0% | 1 unanswerable query | 100.0% | 1 unanswerable query | low sample |
| Answer coverage | 66.7% | 3 expected points | 66.7% | 3 expected points | low sample |

### Ablations

| Run | Recall@10 | MRR | Unsupported claims | Claim count | Answer coverage | Status |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Full pipeline | 100.0% | 0.900 | 0.0% | 2 | 66.7% | low sample |
| No reranker | 100.0% | 0.900 | 0.0% | 2 | 66.7% | low sample |
| Dense only | 100.0% | 0.900 | 0.0% | 2 | 66.7% | low sample |
| Sparse only | 80.0% | 0.800 | 0.0% | 1 | 33.3% | low sample |
| Verifier off | — | — | 50.0% | 4 | 66.7% | low sample |
| Plain RAG | 100.0% | 0.900 | 50.0% | 4 | 66.7% | low sample |

Retrieval uses the same five queries and filters across ranking variants. The verifier-off run reuses the full pipeline drafts, so the report omits a separate retrieval score. Plain RAG uses dense retrieval, the same structured prompt, and no reranking or verifier. Answer coverage uses the same three expected points across variants.

### Operation latency

| Stage | Operation | Sample | p50 (ms) | p95 (ms) | Status |
| --- | --- | --- | ---: | ---: | --- |
| OCR | parse | 2 documents | 157.932 | 167.821 | low sample |
| Retrieval | retrieve | 5 queries | 0.395 | 0.754 | low sample |
| Retrieval | rerank | 5 queries | 0.018 | 0.038 | low sample |
| Answer | retrieve | 4 queries | 0.170 | 0.297 | low sample |
| Answer | rerank | 4 queries | 0.015 | 0.020 | low sample |
| Answer | render search citations | 4 queries | 0.009 | 0.020 | low sample |
| Answer | generate | 4 queries | 0.354 | 0.904 | low sample |
| Answer | prepare | 4 queries | 0.564 | 1.291 | low sample |
| Answer | verify | 4 queries | 0.017 | 0.041 | low sample |
| Extraction | score | 2 documents | 0.096 | 0.156 | low sample |
| Relations | score | 1 gold set batch | 0.095 | 0.095 | low sample |
| Summary | score | 2 requests | 0.006 | 0.010 | low sample |

These are local stage times. Hosted cold starts, browser latency, live LLM latency, and manual search effort require separate measurements.
