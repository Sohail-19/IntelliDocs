"""Canonical text v1: raw block text and row-major tables joined by two newlines."""

from dataclasses import dataclass

from intellidocs_intelligence.models import ParsedDocument, TableBlock, TextBlock


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
    return CanonicalDocument("".join(parts), tuple(spans))
