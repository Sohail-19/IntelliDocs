"""Normalize clause, heading, and reference keys before exact index lookups."""

import re
import unicodedata
from collections.abc import Iterator
from datetime import date

import dateparser  # type: ignore[import-untyped]

from intellidocs_intelligence.models import Chunk
from intellidocs_intelligence.sparse import tokenize

RELATION_KEYS_FIELD = "relation_keys_v1"
_IDENTIFIER_PART = r"""[^\s/.,;:!?…(){}\[\]"“”।–—-]+"""
_IDENTIFIER = re.compile(rf"{_IDENTIFIER_PART}(?:\s*[/.-]\s*{_IDENTIFIER_PART})*")
_NUMERIC_DATE = re.compile(
    r"(?<![\w/-])(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|"
    r"[0-9]{1,2}([./-])[0-9]{1,2}\1[0-9]{4})(?![\w/-])"
)
_QUOTED = re.compile(r'["“]([^"”]+)["”]')
_MAX_WINDOW = 128
_TITLE_WORD_LIMIT = 12


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        str(unicodedata.decimal(character)) if character.isdecimal() else character
        for character in folded
    )


def _number_keys(text: str) -> set[str]:
    keys = set()
    for match in _IDENTIFIER.finditer(_NUMERIC_DATE.sub(" ", text)):
        value = re.sub(r"\s+", "", match.group())
        if 2 <= len(value) <= 80 and any(character.isdecimal() for character in value):
            keys.add("number:" + value)
    return keys


def _label_key(kind: str, label: str) -> str | None:
    words = tokenize(normalize(label))
    if 1 <= len(words) <= _TITLE_WORD_LIMIT and len(label) <= _MAX_WINDOW:
        return kind + ":" + " ".join(words)
    return None


def structure_keys(chunk: Chunk) -> set[str]:
    keys = {key for heading in chunk.heading_path or [] if (key := _label_key("heading", heading))}
    if chunk.clause_number:
        # Keep delimiters inside clause numbers, while ignoring a trailing period.
        number = "".join(normalize(chunk.clause_number).split()).rstrip(".")
        if number:
            keys.add("clause:" + number)
    return keys


def indexed_keys(chunk: Chunk, *, title: str | None = None) -> set[str]:
    keys = structure_keys(chunk)
    if chunk.doc_date:
        keys.add("date:" + chunk.doc_date.isoformat())
    labels = list((chunk.heading_path or [])[:1])
    if chunk.char_start == 0 or title:
        labels.extend(line for line in chunk.text.splitlines()[:2] if len(line) <= _MAX_WINDOW)
    if title:
        labels.append(title)
    for label in [*labels, chunk.doc_id]:
        if key := _label_key("title", label):
            keys.add(key)
        keys.update(_number_keys(normalize(label)))
    return keys


def _word(character: str) -> bool:
    return character.isalnum() or unicodedata.category(character).startswith("M")


def _windows(text: str, cues: tuple[str, ...]) -> Iterator[tuple[str, str]]:
    # Scan each line independently and allow reference qualifiers on either side.
    for line in re.split(r"[\n;।]", normalize(text)):
        line = " ".join(line.split())
        for cue in cues:
            for match in re.finditer(re.escape(cue), line):
                start, end = match.span()
                if (start and _word(line[start - 1])) or (end < len(line) and _word(line[end])):
                    continue
                yield line[max(0, start - _MAX_WINDOW) : start], line[end : end + _MAX_WINDOW]


def _reference_dates(text: str, words: list[str], language: str) -> set[date]:
    dates = set()
    for match in _NUMERIC_DATE.finditer(text):
        value = match.group()
        if match.group(1) is None:
            try:
                dates.add(date.fromisoformat(value))
            except ValueError:
                pass
            continue
        first, second, year = (int(part) for part in value.split(match.group(1)))
        # An ambiguous numeric mention may point to either day. Neither supplies
        # or changes the authoritative indexed date used to order the documents.
        for month, day in ((first, second), (second, first)):
            try:
                dates.add(date(year, month, day))
            except ValueError:
                pass
    for first_word, middle, year_word in zip(words, words[1:], words[2:], strict=False):
        if not (len(year_word) == 4 and year_word.isdecimal()):
            continue
        if not (
            (first_word.isdecimal() and not middle.isdecimal())
            or (middle.isdecimal() and not first_word.isdecimal())
        ):
            continue
        try:
            parsed = dateparser.parse(
                " ".join((first_word, middle, year_word)),
                languages=[language],
                settings={"STRICT_PARSING": True, "PARSERS": ["absolute-time"]},
            )
        except ValueError:  # Supported org languages can exceed dateparser's locales.
            continue
        if parsed is not None:
            dates.add(parsed.date())
    return dates


def reference_keys(chunk: Chunk, cues: tuple[str, ...], language: str) -> set[str]:
    keys: set[str] = set()
    for preceding, following in _windows(chunk.text, cues):
        for index, window in enumerate((preceding, following)):
            keys.update(_number_keys(window))
            for title in _QUOTED.findall(window):
                if key := _label_key("title", title):
                    keys.add(key)
            words = tokenize(window)
            words = words[-_TITLE_WORD_LIMIT:] if index == 0 else words[:_TITLE_WORD_LIMIT]
            if words:
                adjacent = words[-1] if index == 0 else words[0]
                if len(adjacent) >= 3:
                    keys.add("title:" + adjacent)
            # Multiword unquoted titles can be embedded in a longer reference.
            keys.update(
                "title:" + " ".join(words[start:end])
                for start in range(len(words))
                for end in range(start + 2, len(words) + 1)
            )
            keys.update(
                "date:" + day.isoformat() for day in _reference_dates(window, words, language)
            )
    return keys
