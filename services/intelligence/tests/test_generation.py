"""Structured generation through FakeProvider with no network access."""

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from app.generation import (
    ClaimGenerator,
    GenerationConfig,
    GenerationError,
    build_prompt,
    generator_from_env,
)
from intellidocs_intelligence.cache import DiskCache
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.models import Chunk


class ScriptedFakeProvider(FakeProvider):
    def __init__(self, responses: list[dict[str, Any] | ValueError]) -> None:
        super().__init__(model="test-model")
        self.responses = responses
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        response = self.responses[self.calls]
        if isinstance(response, ValueError):
            self.calls += 1
            raise response
        self.response = response
        return super().generate(prompt, schema)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Generation tests must not access a network")

    monkeypatch.setattr(socket.socket, "connect", fail)


@pytest.fixture
def chunks() -> list[Chunk]:
    text = 'विभाग must submit the "report".\n$schema_json'
    return [
        Chunk(
            id="legacy-id",
            chunk_id="chunk-a",
            doc_id="metadata-only-doc",
            department="metadata-only-department",
            text=text,
            page=1,
            char_start=0,
            char_end=len(text),
        )
    ]


def claims() -> dict[str, Any]:
    return {
        "claims": [
            {
                "text": "The department must submit the report.",
                "chunk_id": "chunk-a",
                "quote": 'विभाग must submit the "report".',
            }
        ]
    }


def test_prompt_contains_only_labeled_chunk_text_and_question(chunks: list[Chunk]) -> None:
    query = 'What is required? "$request_json"'
    prompt = build_prompt(query, chunks)
    source = json.loads(prompt.split("REQUEST_JSON\n")[1])
    assert source == {"query": query, "chunks": [{"chunk_id": "chunk-a", "text": chunks[0].text}]}
    assert "metadata-only-doc" not in prompt and "metadata-only-department" not in prompt
    assert 'return {"claims": []}' in prompt
    legacy = chunks[0].model_copy(update={"chunk_id": None})
    assert (
        json.loads(build_prompt(query, [legacy]).split("REQUEST_JSON\n")[1])["chunks"][0][
            "chunk_id"
        ]
        == "legacy-id"
    )


def test_valid_claims_parse_and_output_schema_is_supplied(chunks: list[Chunk]) -> None:
    provider = ScriptedFakeProvider([claims()])
    result = ClaimGenerator(provider).generate("What is required?", chunks)
    assert result.model_dump() == claims()
    assert provider.calls == 1
    assert provider.schemas[0]["required"] == ["claims"]
    assert provider.schemas[0]["$defs"]["GeneratedClaim"]["required"] == [
        "text",
        "chunk_id",
        "quote",
    ]


def test_bad_json_and_invalid_schema_retry_then_cache_final_response(
    tmp_path: Path,
    chunks: list[Chunk],
) -> None:
    provider = ScriptedFakeProvider(
        [
            json.JSONDecodeError("Malformed generated JSON", "not JSON", 0),
            {"claims": [{"text": "missing fields"}]},
            claims(),
        ]
    )
    generator = ClaimGenerator(provider, GenerationConfig(2), DiskCache(tmp_path))
    result = generator.generate("What is required?", chunks)
    assert result.model_dump() == claims() and provider.calls == 3
    assert len(set(provider.prompts)) == 3
    assert all(
        chunks[0].text
        in json.loads(prompt.split("REQUEST_JSON\n")[1].split("\n\nRETRY", 1)[0])["chunks"][0][
            "text"
        ]
        for prompt in provider.prompts
    )
    assert generator.generate("What is required?", chunks) == result
    assert provider.calls == 3
    [cached] = list(tmp_path.glob("*.json"))
    assert json.loads(cached.read_text()) == claims()


@pytest.mark.parametrize("retries", [0, 2])
def test_exhausted_retries_fail_safely_without_caching(
    retries: int,
    tmp_path: Path,
    chunks: list[Chunk],
) -> None:
    provider = FakeProvider(model="test-model", response={"claims": "invalid"})
    generator = ClaimGenerator(provider, GenerationConfig(retries), DiskCache(tmp_path))
    with pytest.raises(GenerationError):
        generator.generate("What is required?", chunks)
    assert provider.calls == retries + 1
    assert list(tmp_path.glob("*.json")) == []


def test_empty_claims_are_a_valid_cached_result_and_no_chunks_skip_provider(
    tmp_path: Path,
    chunks: list[Chunk],
) -> None:
    provider = FakeProvider(model="test-model", response={"claims": []})
    generator = ClaimGenerator(provider, cache=DiskCache(tmp_path))
    assert generator.generate("What is required?", []).claims == []
    assert provider.calls == 0
    assert generator.generate("What is required?", chunks).claims == []
    assert generator.generate("What is required?", chunks).claims == []
    assert provider.calls == 1


def test_disk_cache_survives_new_generator_and_keys_include_context(
    tmp_path: Path,
    chunks: list[Chunk],
) -> None:
    provider = FakeProvider(model="test-model", response=claims())
    first = ClaimGenerator(provider, cache=DiskCache(tmp_path)).generate("query", chunks)
    fresh = FakeProvider(model="test-model", response=claims())
    generator = ClaimGenerator(fresh, cache=DiskCache(tmp_path))
    assert generator.generate("query", chunks) == first and fresh.calls == 0
    generator.generate("different query", chunks)
    assert fresh.calls == 1
    generator.generate("query", [chunks[0].model_copy(update={"text": chunks[0].text + " More."})])
    assert fresh.calls == 2


def test_env_configuration_and_invalid_inputs(
    tmp_path: Path,
    chunks: list[Chunk],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GENERATION_MAX_RETRIES", "0")
    generator = generator_from_env()
    assert generator.config.max_retries == 0
    assert generator.generate("query", []).claims == []
    with pytest.raises(ValueError, match="non-whitespace"):
        generator.generate("  ", chunks)
    with pytest.raises(ValueError, match="unique"):
        generator.generate("query", chunks * 2)
    monkeypatch.setenv("GENERATION_MAX_RETRIES", "-1")
    with pytest.raises(ValueError, match="GENERATION_MAX_RETRIES"):
        generator_from_env()
