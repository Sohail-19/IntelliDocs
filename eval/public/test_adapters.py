import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from app.chunking.canonical import canonical_document
from intellidocs_intelligence.models import ExtractionResult, OrgConfig

from eval.extraction.extraction_metrics import GoldDocument
from eval.public.adapters import adapt_cuad, adapt_sroie, receipt_evidence
from eval.public.prepare import save_corpus
from eval.retrieval.metrics import GoldQuery


def test_cuad_gold_uses_annotation_offsets_and_predictions_use_source_text(tmp_path: Path) -> None:
    quote = "The tenant maintains insurance."
    candidate = "The tenant must file records."
    text = f"{'word ' * 250} {quote} {candidate}"
    item = {
        "id": "insurance-1",
        "question": 'Highlight "Insurance". Details: What insurance is required?',
        "answers": [{"text": quote, "answer_start": text.index(quote)}],
    }
    archive = tmp_path / "cuad.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "test.json",
            json.dumps(
                {
                    "data": [
                        {
                            "title": "Synthetic contract",
                            "paragraphs": [{"context": text, "qas": [item]}],
                        }
                    ]
                }
            ),
        )
    corpus = adapt_cuad(archive, 1)
    query = GoldQuery.model_validate(corpus.retrieval_gold[0])
    expected = GoldDocument.model_validate(corpus.extraction_gold[0]).obligations[0]
    hits = [chunk for chunk in corpus.chunks if query.targets[0].matches(chunk)]
    assert len(hits) == 1 and hits[0].page == 2 and quote in hits[0].text
    assert expected.action == quote and expected.page == 2
    assert quote not in query.query

    org = OrgConfig.model_validate(
        {
            "languages": ["en"],
            "ocr_engine": "tesseract",
            "departments": [],
            "document_types": [],
            "routing_rules": [],
            "obligation_detection": {"en": {"cues": ["must"]}},
        }
    )
    save_corpus(corpus, tmp_path, org)
    record = ExtractionResult.model_validate_json((tmp_path / "predictions.jsonl").read_text())
    assert [obligation.action for obligation in record.obligations] == [candidate]
    citations = record.obligations[0].citations
    assert citations
    citation = citations[0]
    canonical = canonical_document(corpus.sources[0].document)
    assert canonical.text[citation.char_start : citation.char_end] == candidate


@pytest.mark.parametrize(
    ("lines", "field", "value", "expected"),
    [
        (["TOTAL EXCLUDING TAX:", "5.00", "TOTAL (RM):", "5.00"], "total", "5.00", [3]),
        (["TOTAL: 5.00", "TOTAL: 5.00"], "total", "5.00", []),
        (["1 First St.", "Suite 2", "Items"], "address", "1 First St, Suite 2", [0, 1]),
        (["1 First St."], "address", "1 First St, Suite 2", []),
    ],
)
def test_sroie_field_alignment(
    lines: list[str], field: str, value: str, expected: list[int]
) -> None:
    assert receipt_evidence(lines, field, value) == expected


def test_sroie_writes_ocr_pairs_and_no_receipt_fields_as_obligations(tmp_path: Path) -> None:
    words = ["Example shop", "March 3, 2025", "TOTAL: 5.00"]
    annotation = tmp_path / "rows.json"
    annotation.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "row_idx": 0,
                        "row": {
                            "key": "R001",
                            "words": words,
                            "bboxes": [[0, 0, 1, 1]] * len(words),
                            "entities": {
                                "company": words[0],
                                "date": words[1],
                                "address": "Unmapped",
                                "total": "5.00",
                            },
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "R001.jpg").write_bytes(b"local image stub")
    output = tmp_path / "prepared"
    corpus = adapt_sroie(annotation, tmp_path, output, 1)
    assert (output / "ground-truth/en/R001.jpg.txt").read_text().splitlines() == words
    assert (output / "documents/en/R001.jpg").read_bytes() == b"local image stub"
    assert GoldDocument.model_validate(corpus.extraction_gold[0]).obligations == []
    assert corpus.members[0]["fields_without_evidence"] == ["address"]
    query = GoldQuery.model_validate(corpus.retrieval_gold[-1])
    hits = [chunk for chunk in corpus.chunks if query.targets[0].matches(chunk)]
    assert len(hits) == 1 and words[-1] in hits[0].text and "5.00" not in query.query
