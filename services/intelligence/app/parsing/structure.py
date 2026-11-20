"""Preserve syntactic document structure without semantic chunking."""

import re
from collections import defaultdict
from collections.abc import Sequence

from intellidocs_intelligence.models import DocumentStructure, Page, TableBlock, TextBlock

from .languages import detected_language

NUMERIC_CLAUSE = r"\([A-Z]+\)|\d+(?:\.\d+)+\.?|\d+\."
PAGE_NUMBER = re.compile(r"(?:\d+|[ivxlcdm]+)(?:\s*/\s*\d+)?", re.I)


def content_text(page: Page) -> str:
    """Body text for language detection/consumers; furniture is kept separately."""
    return "\n".join(
        " ".join(cell for row in block.rows for cell in row)
        if isinstance(block, TableBlock)
        else block.text
        for block in page.blocks
        if block.type != "header_footer"
    )


def preserve_structure(
    pages: list[Page],
    sizes: dict[int, tuple[float, float]],
    languages: Sequence[str],
    structure: DocumentStructure | None = None,
) -> list[Page]:
    settings = structure or DocumentStructure()
    labels = sorted(set(settings.clause_labels + settings.heading_labels), key=len, reverse=True)
    named = r"(?P<label>" + "|".join(map(re.escape, labels)) + r")\s+(?:\d+(?:\.\d+)*|[IVXLCDM]+)\b"
    clause = re.compile(
        r"^\s*(?P<number>" + (named + "|" if labels else "") + NUMERIC_CLAUSE + r")(?=\s|$)", re.I
    )
    heading_labels = {label.casefold() for label in settings.heading_labels}
    page_numbers = [
        PAGE_NUMBER,
        *(re.compile(pattern, re.I) for pattern in settings.page_number_patterns),
    ]
    output = [page.model_copy(deep=True) for page in pages]
    occurrences: dict[tuple[str, str], set[int]] = defaultdict(set)

    def margin(page: Page, block: TextBlock | TableBlock) -> str:
        height = sizes[page.page][1]
        if block.bbox[3] <= height * 0.10:
            return "top"
        if block.bbox[1] >= height * 0.90:
            return "bottom"
        return ""

    def signature(text: str) -> str:
        return re.sub(r"\d+", "#", " ".join(text.casefold().split()))

    for page in output:
        for block in page.blocks:
            zone = margin(page, block)
            if zone and block.type != "table" and block.text.strip():
                occurrences[zone, signature(block.text)].add(page.page)

    headings: list[tuple[int, str]] = []
    for page in output:
        for order, block in enumerate(page.blocks):
            block.reading_order = order
            zone = margin(page, block)
            repeated = len(occurrences[zone, signature(block.text)]) >= max(2, len(output) / 2)
            if (
                block.type != "table"
                and zone
                and (
                    repeated
                    or any(pattern.fullmatch(block.text.strip()) for pattern in page_numbers)
                )
            ):
                page.blocks[order] = block = TextBlock(
                    type="header_footer",
                    text=block.text,
                    bbox=block.bbox,
                    reading_order=order,
                )
            if block.type == "header_footer":
                block.clause_number = None
                block.heading_level = None
                block.heading_path = []
                continue
            if isinstance(block, TableBlock):
                block.text = ""
            else:
                match = clause.match(block.text)
                if match:
                    block.clause_number = match["number"]
                    if labels and (match["label"] or "").casefold() in heading_labels:
                        block.type = "heading"
                    if block.type not in {"heading", "list_item"}:
                        block.type = "clause"
                if block.type == "heading":
                    number = block.clause_number or ""
                    numeric = re.search(r"\d+(?:\.\d+)*", number)
                    level = block.heading_level or (len(numeric[0].split(".")) if numeric else 1)
                    block.heading_level = level
                    while headings and headings[-1][0] >= level:
                        headings.pop()
                    block.heading_path = [text for _, text in headings]
                    headings.append((level, block.text))
                    continue
            block.heading_path = [text for _, text in headings]
        page.language = detected_language(content_text(page), languages)
    return output
