"""Generate schema-validated draft claims from retrieved chunk text."""

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from intellidocs_intelligence.cache import CachedLLMProvider, DiskCache
from intellidocs_intelligence.llm import LLMProvider, provider_from_env
from intellidocs_intelligence.models import Chunk
from intellidocs_intelligence.runtime import cache_directory

from .models import GenerationResult

PROMPTS = Path(__file__).with_name("prompts")


@dataclass(frozen=True)
class GenerationConfig:
    max_retries: int = 2

    def __post_init__(self) -> None:
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("GENERATION_MAX_RETRIES must be a nonnegative integer")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GenerationConfig":
        values = os.environ if env is None else env
        try:
            return cls(max_retries=int(values.get("GENERATION_MAX_RETRIES", "2")))
        except ValueError as error:
            raise ValueError(f"Invalid generation configuration: {error}") from error


class GenerationError(RuntimeError):
    """Generation exhausted its attempts without a valid response."""


class _ValidatedProvider(LLMProvider):
    """Retry and validate before the cache writes the final response."""

    def __init__(self, provider: LLMProvider, config: GenerationConfig) -> None:
        self.provider = provider
        self.config = config
        self.provider_id = provider.provider_id
        self.model = provider.model

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        original = prompt
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.provider.generate(prompt, schema)
                return GenerationResult.model_validate(response).model_dump(mode="json")
            except ValueError:
                if attempt == self.config.max_retries:
                    raise GenerationError(
                        f"Invalid generated claims after {attempt + 1} attempts"
                    ) from None
                prompt = Template((PROMPTS / "retry.txt").read_text(encoding="utf-8")).substitute(
                    original_prompt=original, attempt=str(attempt + 1)
                )
        raise AssertionError("Generation retry loop exited unexpectedly")


def build_prompt(query: str, chunks: Sequence[Chunk], *, language: str | None = None) -> str:
    if not query.strip():
        raise ValueError("Generation query must contain non-whitespace text")
    sources = [{"chunk_id": chunk.chunk_id or chunk.id, "text": chunk.text} for chunk in chunks]
    if len({source["chunk_id"] for source in sources}) != len(sources):
        raise ValueError("Retrieved chunk IDs must be unique")
    request: dict[str, Any] = {"query": query, "chunks": sources}
    if language is not None:
        request["language"] = language
    return Template((PROMPTS / "claims.txt").read_text(encoding="utf-8")).substitute(
        request_json=json.dumps(request, ensure_ascii=False),
        schema_json=json.dumps(GenerationResult.model_json_schema(), ensure_ascii=False),
    )


class ClaimGenerator:
    def __init__(
        self,
        provider: LLMProvider,
        config: GenerationConfig | None = None,
        cache: DiskCache | None = None,
    ) -> None:
        self.config = config if config is not None else GenerationConfig.from_env()
        validated = _ValidatedProvider(provider, self.config)
        self.provider = CachedLLMProvider(validated, cache) if cache is not None else validated

    def generate(
        self, query: str, chunks: Sequence[Chunk], *, language: str | None = None
    ) -> GenerationResult:
        original = build_prompt(query, chunks, language=language)
        if not chunks:
            return GenerationResult(claims=[])
        schema = GenerationResult.model_json_schema()
        try:
            response = self.provider.generate(original, schema)
            # Cache hits still pass the output schema before reaching the caller.
            return GenerationResult.model_validate(response)
        except ValueError:
            raise GenerationError("Cached generation response is invalid") from None


def generator_from_env() -> ClaimGenerator:
    return ClaimGenerator(
        provider_from_env(), GenerationConfig.from_env(), cache=DiskCache(cache_directory())
    )
