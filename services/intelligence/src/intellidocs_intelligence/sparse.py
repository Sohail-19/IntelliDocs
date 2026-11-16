"""Offline BM25-style term weights; Qdrant supplies collection IDF at query time."""

from __future__ import annotations

import hashlib
import math
import os
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .embeddings import SparseVector


def tokenize(text: str) -> list[str]:
    """Language-neutral words, preserving combining marks (including Hindi matras)."""
    tokens: list[str] = []
    word: list[str] = []
    for character in unicodedata.normalize("NFKC", text).casefold():
        category = unicodedata.category(character)[0]
        if category in {"L", "N"} or (category == "M" and word):
            word.append(character)
        elif character in {"\u200c", "\u200d"}:
            continue  # Join controls do not split an otherwise contiguous word.
        elif word:
            tokens.append("".join(word))
            word = []
    if word:
        tokens.append("".join(word))
    return tokens


def term_id(token: str) -> int:
    return int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:4], "big")


@dataclass(frozen=True)
class Bm25Encoder:
    k1: float = 1.2
    b: float = 0.75
    avg_doc_length: float = 400.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.k1) or self.k1 <= 0:
            raise ValueError("BM25_K1 must be finite and positive")
        if not math.isfinite(self.b) or not 0 <= self.b <= 1:
            raise ValueError("BM25_B must be finite and between 0 and 1")
        if not math.isfinite(self.avg_doc_length) or self.avg_doc_length <= 0:
            raise ValueError("BM25_AVG_DOC_LENGTH must be finite and positive")

    @classmethod
    def from_env(cls) -> Bm25Encoder:
        return cls(
            float(os.environ.get("BM25_K1", "1.2")),
            float(os.environ.get("BM25_B", "0.75")),
            float(os.environ.get("BM25_AVG_DOC_LENGTH", "400")),
        )

    def cache_key(self) -> dict[str, object]:
        return {"encoder": "unicode-sha256-bm25-v1", **asdict(self)}

    def document(self, text: str) -> SparseVector:
        tokens = tokenize(text)
        # Aggregate collisions before weighting, producing unique uint32 indices.
        counts = Counter(term_id(token) for token in tokens)
        indices = sorted(counts)
        normalizer = self.k1 * (1 - self.b + self.b * len(tokens) / self.avg_doc_length)
        return {
            "indices": indices,
            "values": [
                counts[index] * (self.k1 + 1) / (counts[index] + normalizer) for index in indices
            ],
        }

    def query(self, text: str) -> SparseVector:
        """Binary query term weights; IDF is applied once by Qdrant, not here."""
        indices = sorted({term_id(token) for token in tokenize(text)})
        return {"indices": indices, "values": [1.0] * len(indices)}
