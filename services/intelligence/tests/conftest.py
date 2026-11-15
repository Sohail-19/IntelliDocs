"""Isolated ingestion resources shared by endpoint tests."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.parsing.base import DocumentParser, result_for
from intellidocs_intelligence.models import Page, ParseResult, TextBlock


class LocalTestParser(DocumentParser):
    def parse(self, file_path: Path, languages: Sequence[str]) -> ParseResult:
        reader = PdfReader(file_path)
        pages = [
            Page(
                page=index + 1,
                language=languages[0],
                blocks=[
                    TextBlock(
                        type="paragraph",
                        text=page.extract_text() or "Image page",
                        bbox=[0, 0, 10, 10],
                        reading_order=0,
                    )
                ],
            )
            for index, page in enumerate(reader.pages)
        ]
        return result_for(file_path, pages, "offline-test", "1", [])


@pytest.fixture
def ingestion_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("ORG_CONFIG_DIR", str(root / "config/orgs"))
    monkeypatch.setenv("DEFAULT_ORG_ID", "example")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "20971520")
    monkeypatch.setattr("app.ingestion.parser_from_config", lambda _config: LocalTestParser())
    return tmp_path / "storage"
