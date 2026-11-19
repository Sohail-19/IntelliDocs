"""Canonical text v1: raw block text and row-major tables joined by two newlines."""

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from intellidocs_intelligence.models import CitationBox, ParsedDocument, TableBlock, TextBlock


@dataclass(frozen=True)
class BlockSpan:
    block_id: str
    page: int
    language: str
    start: int
    end: int
    block: TableBlock | TextBlock


@dataclass(frozen=True)
class CanonicalDocument:
    text: str
    blocks: tuple[BlockSpan, ...]
    has_pdf_layout: bool = False

    def overlapping_blocks(self, start: int, end: int) -> list[BlockSpan]:
        return [
            span
            for span in self.blocks
            if span.start < end and span.end > start and span.start < span.end
        ]

    def citation_boxes(self, spans: Sequence[BlockSpan]) -> list[CitationBox] | None:
        if not self.has_pdf_layout:
            return None
        boxes = []
        for span in spans:
            x0, y0, x1, y1 = span.block.bbox
            if (
                span.block.type != "header_footer"
                and all(isfinite(value) for value in span.block.bbox)
                and 0 <= x0 < x1
                and 0 <= y0 < y1
            ):
                boxes.append(CitationBox(page=span.page, bbox=span.block.bbox))
        return boxes or None


def canonical_document(document: ParsedDocument) -> CanonicalDocument:
    """Keep furniture in canonical text, but expose spans so it can be excluded safely."""
    parts: list[str] = []
    spans: list[BlockSpan] = []
    pages: set[int] = set()
    offset = 0
    for page in sorted(document.pages, key=lambda item: item.page):
        if page.page in pages:
            raise ValueError(f"Duplicate page number: {page.page}")
        pages.add(page.page)
        orders: set[int] = set()
        for block in sorted(page.blocks, key=lambda item: item.reading_order):
            if block.reading_order in orders:
                raise ValueError(f"Duplicate reading_order on page {page.page}")
            orders.add(block.reading_order)
            text = (
                "\n".join("\t".join(row) for row in block.rows)
                if isinstance(block, TableBlock)
                else block.text
            )
            if spans:
                parts.append("\n\n")
                offset += 2
            start = offset
            parts.append(text)
            offset += len(text)
            spans.append(
                BlockSpan(
                    f"{document.id}:p{page.page}:b{block.reading_order}",
                    page.page,
                    page.language,
                    start,
                    offset,
                    block,
                )
            )
    return CanonicalDocument("".join(parts), tuple(spans), document.mime_type == "application/pdf")
