"""Clause/section-aware chunk windows with whole-sentence overlap."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from intellidocs_intelligence.chunk_ids import chunk_id
from intellidocs_intelligence.models import Chunk, ParsedDocument

from .canonical import BlockSpan, canonical_document
from .config import ChunkingConfig

TERMINAL = re.compile(r"[.!?।॥。！？]+[\"'”’»）)\]]*(?=\s|$)")


def sentence_spans(text: str, abbreviations: Sequence[str] = ()) -> list[tuple[int, int]]:
    """Return whole detected sentences (or trailing fragments), with raw offsets."""
    recognized = {value.casefold().rstrip(".") for value in abbreviations}
    spans: list[tuple[int, int]] = []
    start = 0
    for match in TERMINAL.finditer(text):
        if match[0].startswith("."):
            prefix = text[start : match.start()]
            token = re.search(r"([\w.]+)$", prefix)
            previous = token[0] if token else ""
            if previous.casefold().lstrip(".") in recognized or re.fullmatch(
                r"(?:[^\W\d_]\.)*[^\W\d_]",
                previous,
            ):
                continue
            paragraph_prefix = re.split(r"\n\s*\n", prefix)[-1]
            if re.fullmatch(r"\s*\d+(?:\.\d+)*", paragraph_prefix):
                continue  # A leading clause marker such as 1. or 3.2.
        end = match.end()
        while start < end and text[start].isspace():
            start += 1
        if start < end:
            spans.append((start, end))
        start = end
    while start < len(text) and text[start].isspace():
        start += 1
    end = len(text.rstrip())
    if start < end:
        spans.append((start, end))
    return spans


@dataclass
class Group:
    blocks: list[BlockSpan]
    clause_number: str | None
    heading_path: list[str]


def groups(blocks: tuple[BlockSpan, ...]) -> list[Group]:
    output: list[Group] = []
    current: list[BlockSpan] = []
    clause: str | None = None
    path: list[str] = []
    stack: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal current
        if current:
            output.append(Group(current, clause, list(path)))
            current = []

    for span in blocks:
        block = span.block
        if block.type == "header_footer":
            flush()
            continue
        if span.start == span.end:
            continue
        if current and span.language != current[-1].language:
            flush()
        if block.type == "heading":
            flush()
            level = block.heading_level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            ancestors = (
                block.heading_path
                if block.heading_path is not None
                else [text for _, text in stack]
            )
            path = [*ancestors, block.text]
            stack.append((level, block.text))
            clause = block.clause_number
        elif block.heading_path is not None and block.heading_path != path:
            flush()
            path = list(block.heading_path)
            clause = None
        if block.clause_number is not None and block.clause_number != clause:
            flush()
            clause = block.clause_number
        if block.type == "table":
            flush()
            output.append(Group([span], clause, list(path)))
            continue
        current.append(span)
    flush()
    return output


class Chunker:
    def __init__(self, config: ChunkingConfig) -> None:
        self.config = config

    def chunk(
        self,
        document: ParsedDocument,
        *,
        department: str | None = None,
        doc_date: date | None = None,
        doc_type: str | None = None,
        sentence_abbreviations: Sequence[str] = (),
    ) -> list[Chunk]:
        canonical = canonical_document(document)
        chunks: list[Chunk] = []
        for group in groups(canonical.blocks):
            start, end = group.blocks[0].start, group.blocks[-1].end
            text = canonical.text[start:end]
            if not text.strip():
                continue
            if (
                group.blocks[0].block.type == "table"
                or self.config.count(text) <= self.config.target_tokens
            ):
                windows = [(start, end)]
            else:
                windows = self._windows(canonical.text, group.blocks, sentence_abbreviations)
            for begin, finish in windows:
                sources = [
                    span for span in group.blocks if span.start < finish and span.end > begin
                ]
                identifier = chunk_id(document.id, begin, finish)
                chunks.append(
                    Chunk(
                        id=identifier,
                        chunk_id=identifier,
                        doc_id=document.id,
                        text=canonical.text[begin:finish],
                        char_start=begin,
                        char_end=finish,
                        page=sources[0].page,
                        page_start=sources[0].page,
                        page_end=sources[-1].page,
                        block_ids=[span.block_id for span in sources],
                        clause_number=group.clause_number,
                        heading_path=group.heading_path,
                        language=sources[0].language,
                        department=department,
                        doc_date=doc_date,
                        doc_type=doc_type,
                    )
                )
        return chunks

    def _windows(
        self, text: str, blocks: list[BlockSpan], abbreviations: Sequence[str]
    ) -> list[tuple[int, int]]:
        origin, finish = blocks[0].start, blocks[-1].end
        units = [
            (origin + start, origin + end)
            for start, end in sentence_spans(text[origin:finish], abbreviations)
        ]
        paragraph_ends = {
            block.start + len(text[block.start : block.end].rstrip()) for block in blocks
        }
        output = []
        start, previous_end = 0, 0
        while start < len(units):
            end = start + 1
            while (
                end < len(units)
                and self.config.count(text[units[start][0] : units[end][1]])
                <= self.config.target_tokens
            ):
                end += 1
            # An overlap-heavy window must still include new content. Whole
            # sentences take priority over the soft target in this case.
            end = max(end, previous_end + 1)
            if end < len(units):
                boundaries = [
                    index
                    for index in range(max(start + 1, previous_end + 1), end + 1)
                    if units[index - 1][1] in paragraph_ends
                ]
                if boundaries:
                    end = boundaries[-1]
            output.append((units[start][0], units[end - 1][1]))
            if end == len(units):
                break
            next_start = end
            if self.config.overlap_tokens:
                while next_start > start + 1:
                    next_start -= 1
                    if (
                        self.config.count(text[units[next_start][0] : units[end - 1][1]])
                        >= self.config.overlap_tokens
                    ):
                        break
            previous_end, start = end, next_start
        return output
