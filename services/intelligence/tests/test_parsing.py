"""Offline routing and adapter tests using original generated PDF fixtures."""

import json
import shutil
import socket
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from app.parsing import DoclingParser, DocumentParser, ParsingRouter, TesseractParser
from app.parsing.base import result_for
from app.parsing.factory import parser_from_config
from app.parsing.languages import language_codes
from intellidocs_intelligence.models import OrgConfig, Page, ParseResult, TextBlock

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("Parsing must not use the network")

    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


class RecordingParser(DocumentParser):
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[list[str]] = []

    def parse(self, file_path: Path, languages: Sequence[str]) -> ParseResult:
        self.calls.append(list(languages))
        assert len(PdfReader(file_path).pages) == 1
        return result_for(
            file_path,
            [
                Page(
                    page=1,
                    language=languages[0],
                    blocks=[
                        TextBlock(
                            type="paragraph",
                            text=self.name,
                            bbox=[0, 0, 10, 10],
                            reading_order=0,
                        )
                    ],
                )
            ],
            self.name,
            "test",
            [],
        )


@pytest.mark.parametrize("filename,is_scan", [("digital.pdf", False), ("scanned.pdf", True)])
def test_router_selects_path(filename: str, is_scan: bool) -> None:
    digital, ocr = RecordingParser("digital"), RecordingParser("ocr")
    languages = json.loads((ROOT / "config/orgs/example.yaml").read_text())["languages"]
    result = ParsingRouter(digital, ocr).parse(FIXTURES / filename, languages)
    assert bool(digital.calls) is not is_scan
    assert bool(ocr.calls) is is_scan
    assert (ocr if is_scan else digital).calls == [languages]
    assert result.document.filename == filename


def test_router_handles_mixed_pages(tmp_path: Path) -> None:
    writer = PdfWriter()
    for filename in ("digital.pdf", "scanned.pdf", "digital.pdf"):
        writer.add_page(PdfReader(FIXTURES / filename).pages[0])
    mixed = tmp_path / "mixed.pdf"
    writer.write(mixed)
    digital, ocr = RecordingParser("digital"), RecordingParser("ocr")
    result = ParsingRouter(digital, ocr).parse(mixed, ["en"])
    assert [page.page for page in result.document.pages] == [1, 2, 3]
    assert [page.blocks[0].text for page in result.document.pages] == ["digital", "ocr", "digital"]
    assert len(digital.calls) == 2
    assert len(ocr.calls) == 1


def test_language_mapping_and_errors() -> None:
    assert language_codes(["en", "hi"]) == {"en": "eng", "hi": "hin"}
    with pytest.raises(ValueError, match="Unsupported language"):
        TesseractParser().parse(FIXTURES / "scanned.pdf", ["zz"])
    with pytest.raises(ValueError, match="must not be empty"):
        language_codes([])


def test_factory_reads_engine_and_languages() -> None:
    config = OrgConfig.model_validate_json((ROOT / "config/orgs/example.yaml").read_text())
    assert isinstance(parser_from_config(config), TesseractParser)
    config.ocr_engine = "docling"
    assert isinstance(parser_from_config(config), ParsingRouter)
    config.ocr_engine = "unknown"
    with pytest.raises(ValueError, match="Unsupported OCR engine"):
        parser_from_config(config)


def test_missing_tesseract_language_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TesseractParser, "_run", lambda *_args: "Languages:\neng\n")
    with pytest.raises(ValueError, match="install pack 'hin'"):
        TesseractParser().parse(FIXTURES / "scanned.pdf", ["hi"])


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="System Tesseract is not installed")
def test_real_tesseract_scanned_bounds() -> None:
    result = TesseractParser().parse(FIXTURES / "scanned.pdf", ["en"])
    assert result.document.pages[0].blocks
    assert "fixture" in " ".join(block.text for block in result.document.pages[0].blocks).lower()
    assert_bounds(result, 300, 400)


def assert_bounds(result: ParseResult, width: float, height: float) -> None:
    for page in result.document.pages:
        for order, block in enumerate(page.blocks):
            x0, y0, x1, y1 = block.bbox
            assert 0 <= x0 <= x1 <= width
            assert 0 <= y0 <= y1 <= height
            assert block.reading_order == order


def test_docling_layout_adapter_bounds_and_table(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the adapter with native Docling-shaped objects, without model downloads.
    provenance = SimpleNamespace(
        page_no=1,
        bbox=SimpleNamespace(
            l=-2,
            t=390,
            r=320,
            b=360,
            coord_origin="BOTTOMLEFT",
        ),
    )
    heading = SimpleNamespace(
        label=SimpleNamespace(value="section_header"), prov=[provenance], text="Original heading"
    )
    cell = SimpleNamespace(
        start_row_offset_idx=0,
        end_row_offset_idx=1,
        start_col_offset_idx=0,
        end_col_offset_idx=1,
        text="Original cell",
    )
    table = SimpleNamespace(
        label=SimpleNamespace(value="table"),
        prov=[provenance],
        data=SimpleNamespace(num_rows=1, num_cols=1, table_cells=[cell]),
    )
    numbered_item = SimpleNamespace(
        label=SimpleNamespace(value="list_item"),
        prov=[provenance],
        marker="(a)",
        text="Keep original records.",
    )
    document = SimpleNamespace(
        pages={1: SimpleNamespace(size=SimpleNamespace(width=300, height=400))},
        iterate_items=lambda: [(heading, 0), (table, 0), (numbered_item, 0)],
    )
    converter = SimpleNamespace(convert=lambda _path: SimpleNamespace(document=document))
    monkeypatch.setattr(DoclingParser, "_converter", lambda _self: converter)
    monkeypatch.setattr("app.parsing.docling_parser.version", lambda _name: "test")
    result = DoclingParser().parse(FIXTURES / "digital.pdf", ["en"])
    assert result.document.pages[0].blocks[0].type == "heading"
    assert result.document.pages[0].blocks[0].bbox == [0, 10, 300, 40]
    assert result.document.pages[0].blocks[1].rows == [["Original cell"]]
    assert result.document.pages[0].blocks[1].text == ""
    assert result.document.pages[0].blocks[2].clause_number == "(a)"
    assert result.document.pages[0].blocks[2].text.startswith("(a)")
    assert_bounds(result, 300, 400)


def test_docling_requires_local_models() -> None:
    with pytest.raises(RuntimeError, match="local DOCLING_ARTIFACTS_PATH"):
        DoclingParser().parse(FIXTURES / "digital.pdf", ["en"])


def test_docling_disables_downloads_and_remote_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def options(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    modules = {
        "docling.datamodel.pipeline_options": SimpleNamespace(PdfPipelineOptions=options),
        "docling.datamodel.base_models": SimpleNamespace(InputFormat=SimpleNamespace(PDF="pdf")),
        "docling.document_converter": SimpleNamespace(
            DocumentConverter=options,
            PdfFormatOption=options,
        ),
    }
    monkeypatch.setattr("app.parsing.docling_parser.dependency", lambda name, _extra: modules[name])
    DoclingParser(tmp_path)._converter()
    assert captured["enable_remote_services"] is False
    assert captured["do_ocr"] is False
    assert captured["artifacts_path"] == tmp_path
    import os

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
