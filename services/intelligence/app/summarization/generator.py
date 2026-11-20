"""Build and validate cited summary drafts."""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from string import Template

from pydantic import BaseModel, ConfigDict

from app.generation import GeneratedClaim, GenerationConfig, GenerationError
from intellidocs_intelligence.llm import LLMProvider
from intellidocs_intelligence.models import (
    Chunk,
    NonBlankString,
    SummaryPoint,
    SummaryProfile,
    SummaryRequest,
    SummarySettings,
)

PROMPTS = Path(__file__).with_name("prompts")
PROMPT_VERSION = "audience-summary.v1"


class DraftPoint(GeneratedClaim):
    text: NonBlankString
    chunk_id: NonBlankString
    quote: NonBlankString


class SummaryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    points: list[DraftPoint]


class SummaryGenerator:
    def __init__(self, provider: LLMProvider, config: GenerationConfig | None = None) -> None:
        self.provider = provider
        self.config = config if config is not None else GenerationConfig.from_env()
        self.prompt = (PROMPTS / "points.txt").read_text(encoding="utf-8")
        self.retry = (PROMPTS / "retry.txt").read_text(encoding="utf-8")
        fingerprint = hashlib.sha256((self.prompt + "\0" + self.retry).encode()).hexdigest()
        self.prompt_version = f"{PROMPT_VERSION}:{fingerprint}"

    def generate(
        self,
        request: SummaryRequest,
        profiles: Sequence[SummaryProfile],
        chunks: Sequence[Chunk],
        stored_points: Sequence[SummaryPoint],
        settings: SummarySettings,
        budget: int,
    ) -> SummaryDraft:
        schema = SummaryDraft.model_json_schema()
        context = {
            **request.model_dump(exclude_none=True, exclude={"org_id", "assigned_obligations"}),
            "style": settings.style,
            "remaining_characters": budget,
            "profiles": [profile.model_dump() for profile in profiles],
            "already_covered": [
                {
                    "char_start": citation.char_start,
                    "char_end": citation.char_end,
                    "quote": citation.quote,
                }
                for point in stored_points
                for citation in point.citations
            ],
            "chunks": [
                {"chunk_id": chunk.chunk_id or chunk.id, "text": chunk.text} for chunk in chunks
            ],
        }
        original = Template(self.prompt).substitute(
            schema_json=json.dumps(schema, ensure_ascii=False),
            request_json=json.dumps(context, ensure_ascii=False),
        )
        prompt = original
        for attempt in range(self.config.max_retries + 1):
            try:
                return SummaryDraft.model_validate(self.provider.generate(prompt, schema))
            except ValueError:
                if attempt == self.config.max_retries:
                    raise GenerationError("Invalid summary output after retries") from None
                prompt = Template(self.retry).substitute(
                    original_prompt=original, attempt=str(attempt + 2)
                )
        raise AssertionError("Summary retry loop exited unexpectedly")
