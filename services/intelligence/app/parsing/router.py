"""Inspect and route individual pages, preserving original page numbers."""

from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from intellidocs_intelligence.models import Page, ParseResult

from .base import DocumentParser, dependency, result_for
from .languages import language_codes


class ParsingRouter(DocumentParser):
    def __init__(self, digital: DocumentParser, ocr: DocumentParser) -> None:
        self.digital = digital
        self.ocr = ocr

    def parse(self, file_path: Path, languages: Sequence[str]) -> ParseResult:
        language_codes(languages)
        pdf = dependency("pypdf", "parsing")
        reader = pdf.PdfReader(file_path)
        pages: list[Page] = []
        warnings: list[str] = []
        engines: list[str] = []
        sizes: dict[int, tuple[float, float]] = {}
        with TemporaryDirectory(prefix="intellidocs-route-") as directory:
            for index, page in enumerate(reader.pages):
                width, height = float(page.cropbox.width), float(page.cropbox.height)
                sizes[index + 1] = (height, width) if page.rotation % 180 else (width, height)
                text = page.extract_text() or ""
                parser = self.digital if any(char.isalnum() for char in text) else self.ocr
                page_path = Path(directory) / f"page-{index + 1}.pdf"
                writer = pdf.PdfWriter()
                writer.add_page(page)
                writer.write(page_path)
                parsed = parser.parse(page_path, languages)
                if len(parsed.document.pages) != 1:
                    raise RuntimeError("A routed single-page PDF must produce exactly one page")
                pages.append(parsed.document.pages[0].model_copy(update={"page": index + 1}))
                warnings.extend(f"Page {index + 1}: {warning}" for warning in parsed.warnings)
                engines.append(f"{parsed.parser_name}/{parsed.parser_version}")
        if not pages:
            raise ValueError("PDF contains no pages")
        return result_for(
            file_path,
            pages,
            "page-router",
            "1",
            [
                *warnings,
                "Page engines: " + ", ".join(engines),
            ],
            sizes=sizes,
            languages=languages,
        )
