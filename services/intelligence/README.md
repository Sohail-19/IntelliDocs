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
