"""Recover lines and simple aligned tables from local OCR word coordinates."""

import csv
import io
from dataclasses import dataclass
from statistics import median

from intellidocs_intelligence.models import TableBlock, TextBlock

from .base import bounded_bbox


@dataclass
class Word:
    text: str
    box: list[float]


def ocr_blocks(
    data: str, scale: float, width: float, height: float
) -> list[TableBlock | TextBlock]:
    words = []
    for row in csv.DictReader(io.StringIO(data), delimiter="\t"):
        if row["level"] != "5" or not row["text"].strip():
            continue
        x, y = float(row["left"]), float(row["top"])
        words.append(
            Word(
                row["text"].strip(),
                [
                    x / scale,
                    y / scale,
                    (x + float(row["width"])) / scale,
                    (y + float(row["height"])) / scale,
                ],
            )
        )
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda item: ((item.box[1] + item.box[3]) / 2, item.box[0])):
        if lines:
            previous = lines[-1][0]
            tolerance = max(word.box[3] - word.box[1], previous.box[3] - previous.box[1]) / 2
            if abs(sum(word.box[1::2]) / 2 - sum(previous.box[1::2]) / 2) <= tolerance:
                lines[-1].append(word)
                continue
        lines.append([word])

    # Split widely spaced columns, then require two or more aligned rows. Ordinary
    # prose and isolated large gaps remain text rather than speculative tables.
    cells: list[list[list[Word]]] = []
    for line in lines:
        line.sort(key=lambda item: item.box[0])
        gap = 2 * median(max(1, word.box[3] - word.box[1]) for word in line)
        columns: list[list[Word]] = [[line[0]]]
        for word in line[1:]:
            if word.box[0] - columns[-1][-1].box[2] > gap:
                columns.append([])
            columns[-1].append(word)
        cells.append(columns)

    def box_for(items: list[Word]) -> list[float]:
        return bounded_bbox(
            [
                min(word.box[0] for word in items),
                min(word.box[1] for word in items),
                max(word.box[2] for word in items),
                max(word.box[3] for word in items),
            ],
            width,
            height,
        )

    blocks: list[TableBlock | TextBlock] = []
    index = 0
    while index < len(lines):
        end = index + 1
        if len(cells[index]) >= 2:
            while end < len(lines) and len(cells[end]) == len(cells[index]):
                previous_box = box_for(lines[end - 1])
                row_height = max(1, previous_box[3] - previous_box[1])
                if box_for(lines[end])[1] - previous_box[3] > row_height * 3:
                    break
                if any(
                    abs(a[0].box[0] - b[0].box[0]) > row_height
                    for a, b in zip(cells[index], cells[end], strict=True)
                ):
                    break
                end += 1
        if end - index >= 2:
            blocks.append(
                TableBlock(
                    text="",
                    reading_order=len(blocks),
                    bbox=box_for([word for line in lines[index:end] for word in line]),
                    rows=[
                        [" ".join(word.text for word in cell) for cell in row]
                        for row in cells[index:end]
                    ],
                )
            )
        else:
            blocks.append(
                TextBlock(
                    type="paragraph",
                    text=" ".join(word.text for word in lines[index]),
                    bbox=box_for(lines[index]),
                    reading_order=len(blocks),
                )
            )
        index = end
    return blocks
