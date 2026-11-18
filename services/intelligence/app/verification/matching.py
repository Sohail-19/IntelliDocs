"""Normalized substring matching with offsets into the original Unicode text."""

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedText:
    text: str
    spans: tuple[tuple[int, int], ...]


def normalize(text: str) -> NormalizedText:
    # Normalize complete combining sequences so canonical reordering and
    # decomposed characters retain the bounds of their original source cluster.
    clusters: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if index and not unicodedata.combining(char):
            clusters.append((start, index))
            start = index
    if text:
        clusters.append((start, len(text)))
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    for begin, end in clusters:
        for char in unicodedata.normalize("NFD", text[begin:end]):
            if char.isspace():
                if not chars:
                    continue
                if chars[-1] == " ":
                    spans[-1] = (spans[-1][0], end)
                    continue
                char = " "
            chars.append(char)
            spans.append((begin, end))
    if chars and chars[-1] == " ":
        chars.pop()
        spans.pop()
    return NormalizedText("".join(chars), tuple(spans))


def _raw_span(
    original: str, normalized: NormalizedText, start: int, end: int
) -> tuple[int, int] | None:
    begin, finish = normalized.spans[start][0], normalized.spans[end - 1][1]
    # Reject matches ending inside a decomposed character or combining sequence.
    if normalize(original[begin:finish]).text != normalized.text[start:end]:
        return None
    return begin, finish


def match_quote(text: str, quote: str, threshold: float) -> tuple[int, int] | None:
    source, wanted = normalize(text), normalize(quote).text
    if not wanted:
        return None
    position = source.text.find(wanted)
    while position >= 0:
        match = _raw_span(text, source, position, position + len(wanted))
        if match is not None:
            return match
        position = source.text.find(wanted, position + 1)
    if threshold == 1 or not source.text:
        return None

    # Semi-global Levenshtein distance: align the entire quote to a substring
    # of the source, allowing free text before/after it. Keep only two DP rows.
    previous = [0] * (len(source.text) + 1)
    starts = list(range(len(source.text) + 1))
    for index, char in enumerate(wanted, start=1):
        current, current_starts = [index], [0]
        for column, other in enumerate(source.text, start=1):
            distance, begin = min(
                (previous[column - 1] + (char != other), starts[column - 1]),
                (previous[column] + 1, starts[column]),
                (current[column - 1] + 1, current_starts[column - 1]),
            )
            current.append(distance)
            current_starts.append(begin)
        previous, starts = current, current_starts
    best: tuple[float, int, int] | None = None
    for end, (distance, begin) in enumerate(zip(previous, starts, strict=True)):
        if begin >= end:
            continue
        score = 1 - distance / max(len(wanted), end - begin)
        if score < threshold or _raw_span(text, source, begin, end) is None:
            continue
        candidate = (-score, begin, end)
        if best is None or candidate < best:
            best = candidate
    return None if best is None else _raw_span(text, source, best[1], best[2])
