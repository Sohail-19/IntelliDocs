# Evaluation

Evaluation datasets, fixtures, and quality checks for document-intelligence capabilities.

[OCR evaluation](ocr/README.md) reports per-document and ground-truth-language
CER/WER, with raw edit counts and explicit failure coverage. Its generated JSON
reports live in ignored `eval/results/`; sample data stays under ignored `data/`.

[`index_bench.py`](index_bench.py) indexes ParseResult JSON folders and reports
embedding/load times, process peak RSS, chunk counts, and Qdrant collection size.
See [performance results and reproduction](../docs/performance.md).
