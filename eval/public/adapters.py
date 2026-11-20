"""Convert held-out CUAD and SROIE annotations to local evaluation inputs."""

import json
import re
import shutil
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from app.chunking.canonical import canonical_document
from intellidocs_intelligence.models import Chunk, Page, ParsedDocument, ParseResult, TextBlock
from pydantic import BaseModel

CUAD_CATEGORIES = {
    "Audit Rights",
    "Insurance",
    "Minimum Commitment",
    "Notice Period To Terminate Renewal",
}
CUAD_PASSAGE_CHARS = 1_000
SROIE_QUESTIONS = {
    "company": "Which company issued the receipt?",
    "date": "What is the receipt date?",
    "address": "What is the issuing company's address?",
    "total": "What is the grand total on the receipt?",
}


@dataclass
class Corpus:
    sources: list[ParseResult] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    retrieval_gold: list[dict[str, Any]] = field(default_factory=list)
    extraction_gold: list[dict[str, Any]] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)

    def add_document(
        self, source: ParseResult, obligations: list[dict[str, Any]], *, context_lines: int = 0
    ) -> None:
        self.sources.append(source)
        document = source.document
        self.extraction_gold.append(
            {"doc_id": document.id, "language": document.language, "obligations": obligations}
        )
        canonical = canonical_document(document)
        for i, span in enumerate(canonical.blocks):
            neighbors = canonical.blocks[max(0, i - context_lines) : i + 1]
            start, end = neighbors[0].start, neighbors[-1].end
            self.chunks.append(
                Chunk(
                    id=span.block_id,
                    doc_id=document.id,
                    page=span.page,
                    page_start=span.page,
                    page_end=span.page,
                    char_start=start,
                    char_end=end,
                    text=canonical.text[start:end],
                    clause_number=span.block.clause_number,
                    block_ids=[neighbor.block_id for neighbor in neighbors],
                    language=span.language,
                )
            )


def write_jsonl(path: Path, rows: Iterable[BaseModel | dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            data = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
            handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def _source(
    doc_id: str, filename: str, mime_type: str, pages: list[Page], name: str
) -> ParseResult:
    return ParseResult(
        document=ParsedDocument(
            id=doc_id,
            filename=filename,
            mime_type=mime_type,
            source_uri=f"local:{filename}",
            status="ready",
            language="en",
            pages=pages,
        ),
        parser_name=name,
        parser_version="1",
        warnings=[],
    )


def _query(query_id: str, query: str, doc_id: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": query_id,
        "query": query,
        "language": "en",
        "query_type": "same_language",
        "targets": [{"doc_id": doc_id, **target} for target in targets],
        "filters": {"doc_id": doc_id},
    }


def passages(text: str, size: int = CUAD_PASSAGE_CHARS) -> Iterator[tuple[int, int]]:
    """Cut on whitespace using only source text, preserving original offsets."""
    start = end = None
    for word in re.finditer(r"\S+", text):
        if start is not None and end is not None and word.end() - start > size:
            yield start, end
            start = None
        if start is None:
            start = word.start()
        end = word.end()
    if start is not None and end is not None:
        yield start, end


def adapt_cuad(path: Path, count: int) -> Corpus:
    with ZipFile(path) as archive:
        records = json.loads(archive.read("test.json"))["data"][:count]
    if len(records) != count:
        raise ValueError(f"Requested {count} CUAD contracts, found {len(records)}")
    corpus = Corpus()
    for index, contract in enumerate(records, 1):
        doc_id = f"cuad-test-{index:03d}"
        pages: list[Page] = []
        obligations: list[dict[str, Any]] = []
        seen_obligations: set[tuple[int, str]] = set()
        for paragraph in contract["paragraphs"]:
            text = paragraph["context"]
            ranges = list(passages(text))
            first_page = len(pages) + 1
            pages.extend(
                Page(
                    page=page,
                    language="en",
                    blocks=[
                        TextBlock(
                            type="paragraph",
                            text=text[start:end],
                            bbox=[0, 0, 0, 0],
                            reading_order=0,
                        )
                    ],
                )
                for page, (start, end) in enumerate(ranges, first_page)
            )
            for item in paragraph["qas"]:
                question = item["question"]
                category = question.split('"', 2)[1]
                if category not in CUAD_CATEGORIES or not item["answers"]:
                    continue
                relevant: set[int] = set()
                for answer in item["answers"]:
                    quote = answer["text"]
                    start = answer["answer_start"]
                    end = start + len(quote)
                    if text[start:end] != quote:
                        raise ValueError(f"CUAD passage offset mismatch for {item['id']}")
                    overlaps = [
                        (min(end, high) - max(start, low), page)
                        for page, (low, high) in enumerate(ranges, first_page)
                    ]
                    length, best_page = max(overlaps, key=lambda pair: pair[0])
                    if length <= 0:
                        raise ValueError(f"CUAD answer has no source passage for {item['id']}")
                    minimum = min(40, length)
                    relevant.update(page for overlap, page in overlaps if overlap >= minimum)
                    key = (best_page, quote)
                    if category != "Audit Rights" and key not in seen_obligations:
                        obligations.append(
                            {
                                "action": quote,
                                "page": best_page,
                                "deadline": None,
                                "owner": {"department": None, "role": None},
                            }
                        )
                        seen_obligations.add(key)
                corpus.retrieval_gold.append(
                    _query(
                        item["id"],
                        question.split("Details: ", 1)[-1],
                        doc_id,
                        [{"page": page} for page in sorted(relevant)],
                    )
                )
        corpus.add_document(
            _source(doc_id, f"{doc_id}.txt", "text/plain", pages, "cuad-v1"), obligations
        )
        corpus.members.append({"doc_id": doc_id, "title": contract["title"]})
    return corpus


def _address_text(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())


def receipt_evidence(lines: Sequence[str], field_name: str, value: str) -> list[int]:
    """Align the structured field to OCR annotations; ambiguous matches stay unlabeled."""
    if field_name == "address":
        normalized = [_address_text(line) for line in lines]
        address = _address_text(value)
        if not address:
            return []
        joined = "".join(normalized)
        start = joined.find(address)
        if start < 0 or joined.find(address, start + 1) >= 0:
            return []
        end = start + len(address)
        hits, offset = [], 0
        for index, line in enumerate(normalized):
            if line and offset < end and offset + len(line) > start:
                hits.append(index)
            offset += len(line)
        return hits
    if not value.strip():
        return []
    pattern = re.compile(rf"(?<!\w){re.escape(value)}(?!\w)", re.IGNORECASE)
    matches = [i for i, line in enumerate(lines) if pattern.search(line)]
    if field_name == "total" and len(matches) > 1:
        label = re.compile(r"\s*(?:grand\s+)?total(?:\s*\([a-z]+\))?\s*:?\s*", re.IGNORECASE)
        matches = [
            i
            for i in matches
            if label.fullmatch(pattern.sub("", lines[i], count=1))
            or (i > 0 and label.fullmatch(lines[i - 1]))
        ]
    return matches if len(matches) == 1 else []


def adapt_sroie(annotation: Path, images: Path, output: Path, count: int) -> Corpus:
    entries = json.loads(annotation.read_text(encoding="utf-8"))["rows"][:count]
    if [entry["row_idx"] for entry in entries] != list(range(count)):
        raise ValueError(f"Expected SROIE test rows 0 through {count - 1}")
    documents, truth = (output / subdir / "en" for subdir in ("documents", "ground-truth"))
    documents.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)
    fields = []
    corpus = Corpus()
    for entry in entries:
        row = entry["row"]
        key = row["key"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError(f"Invalid receipt key: {key!r}")
        filename = f"{key}.jpg"
        doc_id = f"sroie-{key}"
        lines: list[str] = []
        blocks: list[TextBlock] = []
        for text, box in zip(row["words"], row["bboxes"], strict=True):
            if text.strip():
                lines.append(text)
                blocks.append(
                    TextBlock(
                        type="paragraph",
                        text=text,
                        bbox=box,
                        reading_order=len(blocks),
                        clause_number=f"line-{len(lines)}",
                    )
                )
        shutil.copyfile(images / filename, documents / filename)
        (truth / f"{filename}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        locations = {}
        for name, question in SROIE_QUESTIONS.items():
            hits = receipt_evidence(lines, name, row["entities"][name])
            locations[name] = [blocks[i].clause_number for i in hits]
            if hits:
                corpus.retrieval_gold.append(
                    _query(
                        f"{doc_id}-{name}",
                        question,
                        doc_id,
                        [{"page": 1, "clause_number": blocks[i].clause_number} for i in hits],
                    )
                )
        fields.append({"doc_id": doc_id, "fields": row["entities"], "evidence_lines": locations})
        corpus.add_document(
            _source(
                doc_id,
                filename,
                "image/jpeg",
                [Page(page=1, language="en", blocks=[*blocks])],
                "sroie",
            ),
            [],
            context_lines=1,
        )
        corpus.members.append(
            {
                "doc_id": doc_id,
                "row": entry["row_idx"],
                "image": filename,
                "fields_without_evidence": [name for name in locations if not locations[name]],
            }
        )
    write_jsonl(output / "receipt_fields.jsonl", fields)
    return corpus
