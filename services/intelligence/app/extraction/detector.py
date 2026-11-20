"""Find obligation candidates using the active organization's language rules."""

import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from app.chunking.chunker import sentence_spans
from intellidocs_intelligence.models import Chunk, OrgConfig

ISO_DATE = re.compile(r"(?<!\w)\d{4}-\d{2}-\d{2}(?!\w)")


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text.casefold()).split())


def _word_character(character: str) -> bool:
    return character.isalnum() or character == "_" or unicodedata.category(character)[0] == "M"


def contains_phrase(text: str, phrase: str) -> bool:
    for match in re.finditer(re.escape(phrase), text):
        begin, end = match.span()
        if begin > 0 and _word_character(text[begin - 1]):
            continue
        if end < len(text) and _word_character(text[end]):
            continue
        return True
    return False


@dataclass(frozen=True)
class CandidateChunk:
    chunk: Chunk
    sentences: tuple[str, ...]


@dataclass(frozen=True)
class _LanguageRules:
    cues: tuple[str, ...]
    date_patterns: tuple[re.Pattern[str], ...]

    def has_cue(self, text: str) -> bool:
        return any(contains_phrase(text, cue) for cue in self.cues)

    def matches(self, text: str) -> bool:
        return self.has_cue(text) or any(pattern.search(text) for pattern in self.date_patterns)


class CandidateDetector:
    def __init__(self, org: OrgConfig) -> None:
        self.abbreviations = org.document_structure.sentence_abbreviations
        self.languages = {language.casefold() for language in org.languages}
        self.rules: dict[str, _LanguageRules] = {}
        for language, rules in org.obligation_detection.items():
            key = language.casefold()
            if key not in self.languages:
                raise ValueError(f"Obligation detection language {language!r} is not in languages")
            try:
                date_patterns = tuple(
                    re.compile(unicodedata.normalize("NFC", pattern), re.IGNORECASE)
                    for pattern in rules.date_patterns
                )
            except re.error as error:
                raise ValueError(f"Invalid date pattern for {language!r}: {error}") from error
            if any(pattern.search("") for pattern in date_patterns):
                raise ValueError(f"Date patterns for {language!r} must not match empty text")
            cues = tuple(_normalize(cue) for cue in rules.cues)
            self.rules[key] = _LanguageRules(cues, date_patterns)

    def _rules_for(self, language: str | None) -> Iterable[_LanguageRules]:
        key = (language or "").casefold()
        if key not in self.languages:
            key = key.split("-", 1)[0]
        if key not in self.languages:
            return self.rules.values()
        rule = self.rules.get(key)
        return () if rule is None else (rule,)

    def has_cue(self, text: str, language: str | None) -> bool:
        normalized = _normalize(text)
        return any(rule.has_cue(normalized) for rule in self._rules_for(language))

    def detect(self, chunks: Iterable[Chunk]) -> Iterator[CandidateChunk]:
        for chunk in chunks:
            rules = self._rules_for(chunk.language)
            sentences = []
            for start, end in sentence_spans(chunk.text, self.abbreviations):
                sentence = chunk.text[start:end]
                normalized = _normalize(sentence)
                if ISO_DATE.search(normalized) or any(rule.matches(normalized) for rule in rules):
                    sentences.append(sentence)
            if sentences:
                yield CandidateChunk(chunk, tuple(sentences))
