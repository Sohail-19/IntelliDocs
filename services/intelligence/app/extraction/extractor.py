"""Extract unverified drafts from a bounded set of candidate chunks."""

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from pydantic import BaseModel, ConfigDict

from intellidocs_intelligence.cache import CachedLLMProvider, DiskCache
from intellidocs_intelligence.llm import LLMProvider, provider_from_env
from intellidocs_intelligence.models import Chunk, NonEmptyString, ObligationType, OrgConfig

from .detector import CandidateDetector

LOGGER = logging.getLogger(__name__)
PROMPTS = Path(__file__).with_name("prompts")


class DraftObligation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chunk_id: NonEmptyString
    action: NonEmptyString
    owner_text: str
    deadline_text: str
    condition: NonEmptyString | None
    obligation_type: ObligationType
    quote: NonEmptyString


class DraftBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    obligations: list[DraftObligation]

    @classmethod
    def from_response(cls, response: dict[str, Any], chunk_ids: set[str]) -> "DraftBatch":
        batch = cls.model_validate(response)
        if any(draft.chunk_id not in chunk_ids for draft in batch.obligations):
            raise ValueError("Obligation response references a chunk outside the batch")
        return batch


@dataclass(frozen=True)
class ExtractionConfig:
    batch_size: int = 4
    max_chunks_per_document: int = 20
    max_retries: int = 1

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("EXTRACTION_BATCH_SIZE", self.batch_size, 1),
            ("EXTRACTION_MAX_CHUNKS_PER_DOCUMENT", self.max_chunks_per_document, 0),
            ("EXTRACTION_MAX_RETRIES", self.max_retries, 0),
        ):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ExtractionConfig":
        values = os.environ if env is None else env
        return cls(
            batch_size=int(values.get("EXTRACTION_BATCH_SIZE", "4")),
            max_chunks_per_document=int(values.get("EXTRACTION_MAX_CHUNKS_PER_DOCUMENT", "20")),
            max_retries=int(values.get("EXTRACTION_MAX_RETRIES", "1")),
        )


class ExtractionError(RuntimeError):
    """A batch did not yield usable structured extraction output."""


class _ValidatedBatchProvider(LLMProvider):
    """Run retries inside the cache so failed responses never become cached successes."""

    def __init__(self, provider: LLMProvider, chunk_ids: set[str], max_retries: int) -> None:
        self.provider = provider
        self.chunk_ids = chunk_ids
        self.max_retries = max_retries
        self.provider_id = provider.provider_id
        self.model = provider.model

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        original = prompt
        for attempt in range(self.max_retries + 1):
            try:
                response = self.provider.generate(prompt, schema)
                return DraftBatch.from_response(response, self.chunk_ids).model_dump(mode="json")
            except ValueError:
                if attempt == self.max_retries:
                    raise ExtractionError(
                        f"Invalid obligation output after {attempt + 1} attempt(s)"
                    ) from None
                retry = Template((PROMPTS / "retry.txt").read_text(encoding="utf-8"))
                prompt = retry.substitute(original_prompt=original, attempt=str(attempt + 1))
        raise AssertionError("Extraction retry loop exited unexpectedly")


class ObligationExtractor:
    def __init__(
        self,
        detector: CandidateDetector,
        provider: LLMProvider,
        config: ExtractionConfig | None = None,
        cache: DiskCache | None = None,
    ) -> None:
        self.detector = detector
        self.provider = provider
        self.config = config if config is not None else ExtractionConfig.from_env()
        self.cache = cache

    def _extract_batch(self, chunks: dict[str, str]) -> list[DraftObligation]:
        schema = DraftBatch.model_json_schema()
        template = Template((PROMPTS / "obligations.txt").read_text(encoding="utf-8"))
        prompt = template.substitute(
            schema_json=json.dumps(schema),
            chunks_json=json.dumps(
                [{"chunk_id": identifier, "text": text} for identifier, text in chunks.items()],
                ensure_ascii=False,
            ),
        )
        chunk_ids = set(chunks)
        provider: LLMProvider = _ValidatedBatchProvider(
            self.provider, chunk_ids, self.config.max_retries
        )
        if self.cache is not None:
            provider = CachedLLMProvider(provider, self.cache)
        try:
            return DraftBatch.from_response(
                provider.generate(prompt, schema), chunk_ids
            ).obligations
        except ValueError:
            raise ExtractionError("Cached obligation output is invalid") from None

    def extract(self, chunks: Sequence[Chunk]) -> list[DraftObligation]:
        """Process one document in input order, with a cap on unique candidate chunks."""
        if not chunks:
            return []
        doc_id = chunks[0].doc_id
        if any(chunk.doc_id != doc_id for chunk in chunks):
            raise ValueError("Extraction expects chunks from a single document")
        selected: dict[str, str] = {}
        seen: set[str] = set()
        candidates = 0
        for candidate in self.detector.detect(chunks):
            candidates += 1
            chunk = candidate.chunk
            identifier = chunk.chunk_id or chunk.id
            if identifier in seen:
                continue
            seen.add(identifier)
            if len(selected) < self.config.max_chunks_per_document:
                selected[identifier] = chunk.text
        LOGGER.info(
            "Obligation candidates for doc_id=%s: scanned=%d skipped_non_candidates=%d "
            "selected=%d skipped_limit=%d",
            doc_id,
            len(chunks),
            len(chunks) - candidates,
            len(selected),
            len(seen) - len(selected),
        )
        fields = list(selected.items())
        drafts = []
        for start in range(0, len(fields), self.config.batch_size):
            drafts.extend(self._extract_batch(dict(fields[start : start + self.config.batch_size])))
        return drafts


def extractor_from_env(org: OrgConfig) -> ObligationExtractor:
    storage = Path(os.environ.get("STORAGE_DIR", "data/uploads"))
    cache = DiskCache(Path(os.environ.get("LLM_CACHE_DIR") or storage / "llm-cache"))
    return ObligationExtractor(
        CandidateDetector(org), provider_from_env(), ExtractionConfig.from_env(), cache
    )
