# OCR evaluation

Run from the repository root using the intelligence service's Python 3.11+ environment:

```sh
uv sync --project services/intelligence --extra tesseract --extra formats
uv run --project services/intelligence python eval/ocr/evaluate.py \
  --documents data/ocr/documents --ground-truth data/ocr/ground-truth \
  --org-config config/orgs/example.yaml
```

Install the system Tesseract packs required by that org config. Docling additionally
requires its optional extra and pre-provisioned `DOCLING_ARTIFACTS_PATH`. Evaluation
uses the existing parser factory and local image/DOCX adapters; it does not use
HTTP, ingestion storage, indexing, or runtime model downloads.

By default, pair `documents/<language>/<name>.<ext>` with
`ground-truth/<language>/<name>.<ext>.txt`. All pages of that document have the
ground-truth language label. Supported inputs: PDF, PNG, JPG/JPEG, DOCX. Keep
language labels consistent with the selected org config; never use the parser's
predicted language to select ground truth. See [data preparation](../../data/README.md).

For multilingual documents, pass `--manifest data/ocr/manifest.json`:

```json
{
  "documents": [{
    "file": "circular.pdf",
    "references": [
      {"language": "hi", "text": "circular-hi.txt", "pages": [1]},
      {"language": "en", "text": "circular-en.txt", "pages": [2]}
    ]
  }]
}
```

Manifest document paths are relative to `--documents`, and text paths to
`--ground-truth`. Page numbers are one-based, must not overlap, and must cover
every parsed page. Missing predicted pages count as deletions. Omit `pages` only
for a single whole-document reference. A genuinely bilingual page can be labeled
`en+hi` with one interleaved transcript; its scores are reported in a separate
`en+hi` group, not falsely attributed to either language. For separate Hindi and
English scores, choose single-language pages or create and transcribe separate
crops (record the source and crop coordinates locally).

## Scoring protocol

- NFC-normalize Unicode and collapse all whitespace to single spaces, trimming
  both ends. Keep case, punctuation, numbering, and spelling as printed.
- CER uses exact Levenshtein edits over Unicode code points, including normalized
  spaces. It is not a grapheme-cluster score; Devanagari combining marks count as
  separate code points. WER uses Unicode whitespace-delimited tokens.
- Each rate is `edits / max(1, reference length)`. Empty/empty is zero; insertions
  against empty ground truth remain counted. Rates may exceed 100%.
- Aggregate edits and reference lengths before division (micro-average), rather
  than averaging document percentages. Scores are in [0, infinity), not capped.
- Read blocks in reading order; serialize table cells once, row-major. Include
  header/footer blocks by default so a full-page transcription can measure all
  visible text. Use `--body-only` only with correspondingly edited ground truth.
  The selected policy is recorded in the JSON.

The terminal prints each document and language plus total CER/WER percentages.
JSON retains fractional rates, edit counts, denominators, detected page languages,
parser identity/version/warnings, Python version, input/config/reference SHA-256
hashes, timings, and coverage counts. It stores no full reference/hypothesis text.
Ordering is deterministic in paired-folder mode. Ground truth must be independent
of the engine under evaluation; layout/reading-order mistakes also affect scores.

Results default to `eval/results/ocr_<UTC-date>.json` (ignored by git). An existing
result is not overwritten unless `--overwrite` is explicit; use `--output PATH`
to keep multiple runs. A failed parse or missing reference produces an error row,
an incomplete report, and exit status 1. Aggregates then cover successful files
only, with scored/expected coverage shown. Invalid setup exits 1 before scoring.

Test the metric implementation without OCR models:

```sh
services/intelligence/.venv/bin/python -m unittest discover -s eval/ocr -p 'test_*.py'
services/intelligence/.venv/bin/ruff check eval/ocr
MYPYPATH=services/intelligence/src:services/intelligence \
  services/intelligence/.venv/bin/mypy --strict eval/ocr/evaluate.py
```

Do not compare the scores of native-text extraction with scanned OCR as if they
were the same task. Keep a fixed sample, source versions, transcription policy,
and parser/model versions when comparing runs.
