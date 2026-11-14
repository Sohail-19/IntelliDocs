"""Provider-neutral structured LLM interfaces and provider factory."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any
from urllib.request import Request, urlopen


class LLMProvider(ABC):
    """An LLM capable of returning an object constrained by a JSON Schema."""

    provider_id: str
    model: str

    @abstractmethod
    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Generate one schema-conforming object for a prompt."""


class FakeProvider(LLMProvider):
    """Deterministic provider for local development and tests."""

    def __init__(self, model: str, response: dict[str, Any] | None = None) -> None:
        self.provider_id = ""
        self.model = model
        self.response = response or {}
        self.calls = 0

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        self.calls += 1
        return self.response.copy()


class GeminiProvider(LLMProvider):
    """Adapter for the Gemini generate-content API."""

    def __init__(self, provider_id: str, model: str, api_key: str) -> None:
        self.provider_id = provider_id
        self.model = model
        self.api_key = api_key

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
        }
        response = _post_json(endpoint, payload, {})
        text = response["candidates"][0]["content"]["parts"][0]["text"]
        return _as_object(text)


class OpenAICompatibleProvider(LLMProvider):
    """Adapter for OpenAI-compatible chat-completions endpoints."""

    def __init__(self, provider_id: str, model: str, api_key: str, base_url: str) -> None:
        self.provider_id = provider_id
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": schema},
            },
        }
        response = _post_json(
            f"{self.base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
        )
        return _as_object(response["choices"][0]["message"]["content"])


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - URL is configured by the operator.
        body = response.read().decode("utf-8")
    decoded = json.loads(body)
    if not isinstance(decoded, dict):
        raise ValueError("LLM response must be a JSON object")
    return decoded


def _as_object(value: Any) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ValueError("LLM generated content must be a JSON object")
    return decoded


def provider_from_env(env: dict[str, str] | None = None) -> LLMProvider:
    """Create the configured provider without leaking provider choice to callers."""
    settings = os.environ if env is None else env
    provider = settings.get("LLM_PROVIDER", "fake")
    model = settings.get("LLM_MODEL", "test-model")
    api_key = settings.get("LLM_API_KEY", "")

    if provider == "fake":
        instance = FakeProvider(model=model)
        instance.provider_id = provider
        return instance
    if provider == "gemini":
        return GeminiProvider(provider, model, _required(api_key, "LLM_API_KEY"))
    if provider == "openai_compatible":
        return OpenAICompatibleProvider(
            provider,
            model,
            _required(api_key, "LLM_API_KEY"),
            _required(settings.get("LLM_BASE_URL", ""), "LLM_BASE_URL"),
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")


def _required(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} is required for the configured LLM provider")
    return value
