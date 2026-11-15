"""Structural preservation tests; all text and fixtures are original."""

import shutil
from pathlib import Path

import pytest

from app.parsing import TesseractParser
from app.parsing.ocr_structure import ocr_blocks
from app.parsing.structure import content_text, preserve_structure
from intellidocs_intelligence.models import Page, TextBlock


def block(text: str, y: float = 100, **fields: object) -> TextBlock:
    return TextBlock.model_validate(
        {
            "type": "paragraph",
            "text": text,
            "bbox": [20, y, 280, y + 15],
            "reading_order": 0,
            **fields,
        }
    )


@pytest.mark.parametrize(
    "prefix", ["1.", "1.1", "1.1.", "(a)", "Clause 4.2", "Article 5", "Section 3"]
)
def test_clause_numbers(prefix: str) -> None:
    pages = [Page(page=1, language="en", blocks=[block(f"{prefix} Original fixture text.")])]
    result = preserve_structure(pages, {1: (300, 400)}, ["en"])
    assert result[0].blocks[0].clause_number == prefix


def test_nested_heading_paths_across_pages() -> None:
    pages = [
        Page(
            page=1,
            language="en",
            blocks=[
                block("1. Terms", type="heading"),
                block("1.1 Delivery", type="heading"),
                block("(a) Deliver on time."),
            ],
        ),
        Page(
            page=2,
            language="en",
            blocks=[
                block("1.1.1 Reports", type="heading"),
                block("Provide a monthly report."),
                block("Appendix", type="heading", heading_level=1),
                block("Original appendix text."),
            ],
        ),
    ]
    result = preserve_structure(pages, {1: (300, 400), 2: (300, 400)}, ["en"])
    assert result[0].blocks[1].heading_level == 2
    assert result[0].blocks[2].heading_path == ["1. Terms", "1.1 Delivery"]
    assert result[1].blocks[0].heading_level == 3
    assert result[1].blocks[1].heading_path == ["1. Terms", "1.1 Delivery", "1.1.1 Reports"]
    assert result[1].blocks[-1].heading_path == ["Appendix"]
    assert pages[0].blocks[0].heading_level is None  # Inputs are not mutated.


def test_furniture_and_language_are_page_specific() -> None:
    hindi = "यह एक उदाहरण दस्तावेज है। कृपया सभी आवश्यक जानकारी और विवरण ध्यान से पढ़ें।"
    english = "This is an original example document. Please read all information carefully."
    pages = [
        Page(
            page=index,
            language="en",
            blocks=[
                block("ORIGINAL FIXTURE AGREEMENT", y=10),
                block(text, y=120),
                block("Repeated body sentence.", y=200),
                block(f"Page {index} of 2", y=375),
            ],
        )
        for index, text in enumerate([hindi, english], 1)
    ]
    result = preserve_structure(pages, {1: (300, 400), 2: (300, 400)}, ["en", "hi"])
    assert [page.language for page in result] == ["hi", "en"]
    for page in result:
        assert page.blocks[0].type == "header_footer"
        assert page.blocks[-1].type == "header_footer"
        assert page.blocks[2].type == "paragraph"
        assert "ORIGINAL FIXTURE" not in content_text(page)
        assert "Repeated body sentence." in content_text(page)


def test_ocr_table_cells_and_clause_lines_without_system_ocr() -> None:
    data = "level\tleft\ttop\twidth\theight\ttext\n" + "\n".join(
        [
            "5\t20\t50\t12\t10\t1.1",
            "5\t40\t50\t50\t10\tDeliver",
            "5\t20\t100\t30\t10\tItem",
            "5\t200\t100\t40\t10\tCount",
            "5\t20\t120\t45\t10\tReports",
            "5\t200\t120\t15\t10\t12",
        ]
    )
    blocks = ocr_blocks(data, 1, 300, 400)
    assert len(blocks) == 2
    assert blocks[0].text == "1.1 Deliver"
    assert blocks[1].rows == [["Item", "Count"], ["Reports", "12"]]
    assert blocks[1].text == ""


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="System Tesseract is not installed")
def test_structured_scanned_fixture() -> None:
    path = Path(__file__).parent / "fixtures/structured-scanned.pdf"
    result = TesseractParser().parse(path, ["en"])
    numbers = {block.clause_number for page in result.document.pages for block in page.blocks}
    assert {"1.", "1.1", "(a)", "Clause 4.2", "Article 5", "Section 3"} <= numbers
    for page in result.document.pages:
        tables = [block for block in page.blocks if block.type == "table"]
        assert len(tables) == 1
        assert tables[0].rows == [["Item", "Count"], ["Reports", "12"], ["Notices", "3"]]
        assert tables[0].text == ""
        assert "AGREEMENT" not in content_text(page)
        assert "Page " not in content_text(page)
        assert page.language == "en"
        for order, item in enumerate(page.blocks):
            assert item.reading_order == order
            x0, y0, x1, y1 = item.bbox
            assert 0 <= x0 <= x1 <= 600
            assert 0 <= y0 <= y1 <= 800
