from pathlib import Path

from intellidocs_intelligence.cache import CachedLLMProvider, DiskCache
from intellidocs_intelligence.llm import (
    FakeProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    provider_from_env,
)


def test_factory_selects_fake_provider() -> None:
    provider = provider_from_env({"LLM_PROVIDER": "fake", "LLM_MODEL": "local"})

    assert isinstance(provider, FakeProvider)
    assert provider.model == "local"


def test_factory_selects_gemini_provider() -> None:
    provider = provider_from_env(
        {"LLM_PROVIDER": "gemini", "LLM_MODEL": "model-a", "LLM_API_KEY": "test-key"}
    )

    assert isinstance(provider, GeminiProvider)
    assert provider.model == "model-a"


def test_factory_selects_openai_compatible_provider() -> None:
    provider = provider_from_env(
        {
            "LLM_PROVIDER": "openai_compatible",
            "LLM_MODEL": "model-b",
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "https://llm.example/v1",
        }
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "model-b"


def test_cache_reuses_stored_value(tmp_path: Path) -> None:
    provider = FakeProvider(model="test-model", response={"answer": "cached"})
    cached_provider = CachedLLMProvider(provider, DiskCache(tmp_path))
    schema = {"type": "object"}

    first = cached_provider.generate("hello", schema)
    second = cached_provider.generate("hello", schema)

    assert first == second == {"answer": "cached"}
    assert provider.calls == 1
