"""Disk-backed cache for repeatable LLM calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .llm import LLMProvider


class DiskCache:
    """Store JSON responses as content-addressed files."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.directory / f"{key}.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Cached value at {path} is not an object")
        return value

    def set(self, key: str, value: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{key}.json"
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


class CachedLLMProvider(LLMProvider):
    """Wrap a provider and cache calls by provider, model, prompt, and schema."""

    def __init__(self, provider: LLMProvider, cache: DiskCache) -> None:
        self.provider = provider
        self.cache = cache
        self.provider_id = provider.provider_id
        self.model = provider.model

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        key = self._key(prompt, schema)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        response = self.provider.generate(prompt, schema)
        self.cache.set(key, response)
        return response

    def _key(self, prompt: str, schema: dict[str, Any]) -> str:
        material = json.dumps(
            {"provider": self.provider_id, "model": self.model, "prompt": prompt, "schema": schema},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
