"""Explicit chunk size, overlap, and counting configuration."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkingConfig:
    target_tokens: int
    overlap_tokens: int
    token_count_method: str

    def __post_init__(self) -> None:
        if type(self.target_tokens) is not int or self.target_tokens <= 0:
            raise ValueError("target_tokens must be a positive integer")
        if (
            type(self.overlap_tokens) is not int
            or not 0 <= self.overlap_tokens < self.target_tokens
        ):
            raise ValueError("overlap_tokens must be >= 0 and smaller than target_tokens")
        if self.token_count_method not in {"whitespace", "unicode_words", "characters"}:
            raise ValueError(f"Unsupported token counting method: {self.token_count_method!r}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ChunkingConfig":
        values = os.environ if env is None else env
        names = ("CHUNK_TARGET_TOKENS", "CHUNK_OVERLAP_TOKENS", "CHUNK_TOKEN_COUNT_METHOD")
        missing = [name for name in names if not values.get(name)]
        if missing:
            raise ValueError(f"Missing chunking configuration: {', '.join(missing)}")
        try:
            return cls(int(values[names[0]]), int(values[names[1]]), values[names[2]])
        except ValueError as error:
            raise ValueError(f"Invalid chunking configuration: {error}") from error

    def count(self, text: str) -> int:
        if self.token_count_method == "characters":
            return len(text)
        if self.token_count_method == "whitespace":
            return len(text.split())
        return len(re.findall(r"\w+(?:['’]\w+)*|[^\w\s]", text, flags=re.UNICODE))
