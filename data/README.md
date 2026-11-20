# Local evaluation data

Everything under `data/` except this README is ignored by git. Keep downloads,
documents, annotation drafts, final ground truth, and provenance here. Generated
evaluation reports belong in ignored `eval/results/`. Do not force-add either.

## A small public sample: FUNSD

Use three English test images initially. FUNSD provides scanned forms with JSON
annotations for `form[].text`, word boxes, and entity labels. EPFL-LTS5's custom
terms limit FUNSD to non-commercial research and educational use. Check any
separate rights for the RVL-CDIP images before use.
[Dataset description](https://guillaumejaume.github.io/FUNSD/description/)
[Licence terms](https://guillaumejaume.github.io/FUNSD/work/)
[Official download](https://guillaumejaume.github.io/FUNSD/download/)

From the repository root:

```sh
mkdir -p data/raw data/ocr/documents/en data/ocr/drafts/en data/ocr/ground-truth/en
curl --fail --location https://guillaumejaume.github.io/FUNSD/dataset.zip \
  --output data/raw/funsd.zip
unzip -q data/raw/funsd.zip -d data/raw/funsd
```

Keep a fixed sample: the first three sorted filenames from
`data/raw/funsd/dataset/testing_data/images/`. The following prepares annotation
drafts, not automatically trusted ground truth:

```sh
python3 - <<'PY'
import json
import shutil
from pathlib import Path

source = Path('data/raw/funsd/dataset/testing_data')
for image in sorted((source / 'images').glob('*.png'))[:3]:
    annotation = json.loads((source / 'annotations' / f'{image.stem}.json').read_text())
    entities = sorted(annotation['form'], key=lambda entity: (entity['box'][1], entity['box'][0]))
    text = '\n'.join(entity['text'] for entity in entities if entity['text'].strip())
    shutil.copyfile(image, Path('data/ocr/documents/en') / image.name)
    (Path('data/ocr/drafts/en') / f'{image.name}.txt').write_text(text + '\n', encoding='utf-8')
PY
```

Open each image and review its draft. Entity boxes alone do not establish a
perfect reading order, and annotations can contain mistakes or omit text.
Correct against the image, record the chosen column/row order, and include all
visible text for the default evaluation policy. Copy only reviewed drafts into
`data/ocr/ground-truth/en/`, retaining names such as `0000971160.png.txt`.
The added extension avoids ambiguity if a PDF and PNG share a stem.

## Hindi/English public documents

For two small official Hindi/English notices, the Ministry of Social Justice and
Empowerment's [archive](https://socialjustice.gov.in/whats-new/1242/archive)
lists English and Hindi advertisements for the 2016 National Awards. Open the
two matching View links, download their PDFs, and keep just one or two short
pages from each as a fixed local sample. Record original page numbers if pages
are extracted. Its [reuse policy](https://socialjustice.gov.in/common/1381)
allows specified reuse with attribution and excludes third-party material;
record that permission, rather than calling all government PDFs public domain.

If your sample must be strictly public domain, instead select an item with an
explicit public-domain rights statement from the Library of Congress
[South Asian Digital Collection](https://www.loc.gov/collections/south-asian-digital-collection/).
Inspect its item-level Rights & Access, choose short Hindi/English pages, and
use the item's Download controls for PDF/JPEG. The collection's
[rights statement](https://www.loc.gov/collections/south-asian-digital-collection/about-this-collection/rights-and-access/)
also includes items with *no known restrictions*, which is not an explicit
public-domain determination. Do not substitute arbitrary government circulars
without checking their own reuse terms.

Put single-language samples under `documents/hi/` or `documents/en/`, with
matching UTF-8 `.pdf.txt`/`.jpg.txt` transcriptions under `ground-truth/hi/` or
`ground-truth/en/`. These directories are dataset labels, not runtime language
configuration. Ensure `languages` in the chosen `config/orgs/*.yaml` includes
the evaluated languages and that its OCR engine and language packs are installed.

## Ground truth and bilingual pages

Transcribe from the rendered source, never from the OCR engine being evaluated.
Keep original spelling, case, punctuation, clause numbers, and visible mistakes.
Use UTF-8. Separate blocks by newlines and table cells in row-major order with
spaces; do not repeat a flattened copy of a table. Review unclear text with a
second reader, or exclude that sample before freezing the evaluation set.

The evaluator applies NFC Unicode normalization and whitespace folding only.
Keep page headers, footers, and page numbers unless you deliberately use
`--body-only` and remove exactly that material from the reference too. Do not
silently remove difficult handwritten, stamped, or low-quality content. Mark
exclusions in provenance and keep the same scope across compared engines.

For a PDF with Hindi on page 1 and English on page 2, put independent transcripts
in `ground-truth/circular-hi.txt` and `ground-truth/circular-en.txt`, then create
`data/ocr/manifest.json`:

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

Place the PDF directly under `documents/` for this manifest. For both languages
on the same page, either transcribe the entire page once with label `en+hi`, or
make language-specific image crops with separate references. Do not score the
same full page twice against two partial-language transcripts.

Keep `data/ocr/provenance.json` locally with source URL, download date, applicable
license/permission URL, original filename/page/crop, sample selection method,
transcriber/reviewer, scope exclusions, and SHA-256 checksums. Freeze the sample
before comparing engines. The evaluator records actual input/reference/config
hashes, but cannot certify that a transcription is correct.

Run the evaluator as described in [eval/ocr/README.md](../eval/ocr/README.md).
No dataset download is part of the evaluation runtime, and this change commits
neither sample data nor generated benchmark results.

For document classification, see the [small RVL-CDIP sampling guide](../eval/classification/README.md).

## CUAD and SROIE evaluation subsets

- CUAD v1 comes from The Atticus Project under CC BY 4.0.
  Keep the dataset attribution and review the publisher's notes about the underlying
  contracts. Use the first three contracts in the publisher's `test.json` order.
  [Publisher licence](https://www.atticusprojectai.org/legal/)
  [Publisher dataset and documentation](https://github.com/TheAtticusProject/cuad)
- The ICDAR 2019 SROIE release and the `jsdnrs` redistribution used here are CC BY 4.0.
  Credit the challenge authors and the redistribution. Use `default/test` rows 0–2
  (`X00016469670`, `X00016469671`, `X51005200931`). Receipts can contain personal
  details. Keep the images and derived annotations under ignored `data/`.
  [Licence information](https://github.com/jsdnrs/ICDAR2019-SROIE/blob/main/DATASET.md#licensing-information)
  [Dataset card and test split](https://huggingface.co/datasets/jsdnrs/ICDAR2019-SROIE)

From the repository root, download the original CUAD archive and three SROIE
rows. The dataset viewer gives short-lived image URLs, so fetch the images
immediately after fetching the annotations.

```sh
mkdir -p data/public/raw
curl -fsSL https://raw.githubusercontent.com/TheAtticusProject/cuad/master/data.zip \
  -o data/public/raw/cuad-data.zip
curl -fsSL 'https://datasets-server.huggingface.co/rows?dataset=jsdnrs/ICDAR2019-SROIE&config=default&split=test&offset=0&length=3' \
  -o data/public/raw/sroie-test.json
python3 - <<'PY'
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

raw = Path('data/public/raw')
rows = json.loads((raw / 'sroie-test.json').read_text())['rows']
for entry in rows:
    row = entry['row']
    key, url = row['key'], row['image']['src']
    parsed = urlsplit(url)
    if not key.isalnum() or (parsed.scheme, parsed.hostname) != (
        'https', 'datasets-server.huggingface.co'
    ):
        raise ValueError(f'Unexpected SROIE asset for {key!r}')
    with urlopen(url, timeout=30) as response:
        (raw / f'{key}.jpg').write_bytes(response.read())
PY
```

The CUAD archive used for this run had SHA-256
`f8161d18bea4e9c05e78fa6dda61c19c846fb8087ea969c172753bc2f45b999a`.
The prepared gold sets record the selected contract titles, receipt IDs, and
SHA-256 of all inputs. See the [public evaluation commands](../eval/README.md#public-dataset-evaluation).
