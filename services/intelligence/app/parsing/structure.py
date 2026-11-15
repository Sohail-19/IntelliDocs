"""Preserve syntactic document structure without semantic chunking."""

import re
from collections import defaultdict
from collections.abc import Sequence

from intellidocs_intelligence.models import Page, TableBlock, TextBlock

from .languages import detected_language

CLAUSE = re.compile(
    r"^\s*(?P<number>(?:Clause|Article|Section)\s+(?:\d+(?:\.\d+)*|[IVXLCDM]+)\b"
    r"|\([a-zA-Zivxlcdm]+\)|\d+(?:\.\d+)+\.?|\d+\.)(?=\s|$)",
    re.IGNORECASE,
)
PAGE_NUMBER = re.compile(r"^(?:page\s+)?(?:\d+|[ivxlcdm]+)(?:\s+(?:of|/)\s+\d+)?$", re.I)


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
) -> list[Page]:
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
                and (repeated or PAGE_NUMBER.fullmatch(block.text.strip()))
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
                match = CLAUSE.match(block.text)
                if match:
                    block.clause_number = match["number"]
                    if re.match(r"^(Article|Section)\s", match["number"], re.I):
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
