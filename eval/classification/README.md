# Document type classification

## Train and evaluate

Use `train/` and `test/` folders below an ignored `data/` directory. Put one
`ParseResult` v1 JSON file per document in the appropriate folder. Set its
`document.doc_type` to the reviewed label and use `document.language` or the
per-page language tags. For a document without a top-level tag, the evaluator
uses the most frequent page language. Both labels and languages must come from
the selected organization config. Train with at least two configured types
and an example of every type. Keep file IDs and parsed text disjoint between
the two splits.

From the repo root, using the Intelligence environment:

```sh
services/intelligence/.venv/bin/python -m eval.classification.train data/classification/parsed \
  --org-config config/orgs/example2.yaml
```

The script trains Unicode word TF-IDF and a multinomial logistic regression
model on the training split. It evaluates only against held-out documents
and applies the org's confidence threshold. An abstention counts as a missed
prediction. The report contains overall and per-language accuracy and macro-F1,
and each type's support, accuracy among its gold examples, and one-vs-rest
F1. Missing denominators appear as `null`. Type F1 penalizes predictions on
other types as well as missed examples of the type.

The default JSON artifact is `models/classification/<org>.json`. The report
is `eval/results/classification_<org>.json`. Both locations are gitignored.
`--model` and `--report` accept alternate output paths.

## Small RVL-CDIP text subset

The original RVL-CDIP corpus contains scanned pages, and its dataset card
identifies English as the primary language. The OCR Parquet mirror provides
text and category labels as separate columns. Use streaming projection to
collect a small sample without fetching the full image archive. Keep the
upstream split boundary and review labels and language tags before training.
[Original dataset](https://adamharley.com/rvl-cdip/), [dataset card](https://huggingface.co/datasets/aharley/rvl_cdip), [OCR mirror](https://huggingface.co/datasets/RIPS-Goog-23/RVL-CDIP), [streaming documentation](https://huggingface.co/docs/datasets/main/en/stream)

1. Create a local `data/classification/rvl-org.yaml` with `languages: [en]`,
   `ocr_engine: tesseract`, `document_types: [invoice, scientific report]`,
   `departments: []`, and `routing_rules: []`. Replace these labels with any
   compatible types you want to measure and use the same labels in both splits.
2. Run the following optional download step from the repo root. It writes
   only selected OCR text and labels. Review source usage terms and keep both
   the text and resulting model local. [Source copyright information](https://www.industrydocuments.ucsf.edu/help/copyright/)

   ```sh
   uv run --project services/intelligence --with datasets python - <<'PY'
   from ast import literal_eval
   from collections import Counter
   from itertools import islice
   from pathlib import Path
   import json
   import yaml
   from datasets import load_dataset

   root = Path('data/classification')
   config = yaml.safe_load((root / 'rvl-org.yaml').read_text(encoding='utf-8'))
   labels = config['document_types']
   for split, quota in [('train', 12), ('test', 5)]:
       folder = root / 'parsed' / split
       folder.mkdir(parents=True, exist_ok=True)
       counts = Counter()
       rows = load_dataset('RIPS-Goog-23/RVL-CDIP', split=split, streaming=True,
                           columns=['text', 'label'])
       for index, row in enumerate(islice(rows, 2000)):
           label = row['label']
           if label not in labels or counts[label] >= quota:
               continue
           words = row['text']
           if isinstance(words, str) and words.lstrip().startswith('['):
               words = literal_eval(words)
           text = words.strip() if isinstance(words, str) else ' '.join(words).strip()
           if not text:
               continue
           identifier = f'rvl-{split}-{index}'
           result = {
               'document': {
                   'id': identifier, 'filename': f'{identifier}.tif',
                   'source_uri': f'rvl-cdip://{split}/{index}', 'mime_type': 'image/tiff',
                   'doc_type': label, 'status': 'ready', 'pages': [{
                       'page': 1, 'language': config['languages'][0], 'blocks': [{
                           'type': 'paragraph', 'text': text, 'bbox': [0, 0, 0, 0],
                           'reading_order': 0,
                       }],
                   }],
               },
               'parser_name': 'rvl-cdip-ocr-import', 'parser_version': '1', 'warnings': [],
           }
           (folder / f'{identifier}.json').write_text(json.dumps(result), encoding='utf-8')
           counts[label] += 1
           if all(counts[label] == quota for label in labels):
               break
       print(split, dict(counts))
       if any(counts[label] < quota for label in labels):
           raise SystemExit('The sample limit was reached before all classes filled')
   PY
   ```
3. Run the training command with `--org-config data/classification/rvl-org.yaml`.
   Add independently reviewed examples in other configured languages to measure
   multilingual performance. Use the repo's normal ingestion parser to create
   ParseResults when sampling original page images or local documents.

## Enable suggestions during ingestion

Add an optional `classification` section to an existing org file. The artifact
path is relative to the Intelligence process's working directory unless
absolute. The artifact labels must exactly match `document_types`.

```yaml
classification:
  backend: tfidf
  model_path: models/classification/example2.json
  min_confidence: 0.55
  llm_fallback: false
```

`backend: llm` uses the configured LLM directly. With `backend: tfidf`, setting
`llm_fallback: true` uses the same LLM when the artifact is unavailable or the
text classifier abstains. Use `backend: disabled` or omit the section to disable
suggestions. Both backends receive parsed body text, capped at 16,000 characters.
The LLM receives only the org's configured type labels and may abstain.

A successful suggestion is saved in the ingested ParseResult as `predicted_type`
and `prediction_confidence`. The caller's `doc_type` remains the authoritative
value for indexing and routing. The classifier's confidence is a model score;
use the held-out report to judge its performance for your organization.
