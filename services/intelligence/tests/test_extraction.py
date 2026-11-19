"""Candidate filtering and bounded LLM calls with a fake provider."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from app.extraction import (
    CandidateDetector,
    ExtractionConfig,
    ExtractionError,
    ObligationExtractor,
    extractor_from_env,
)
from app.ingestion import load_org
from intellidocs_intelligence.cache import DiskCache
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.models import Chunk, ObligationDetectionRules, OrgConfig


@pytest.fixture
def org(monkeypatch: pytest.MonkeyPatch) -> OrgConfig:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("ORG_CONFIG_DIR", str(root / "config/orgs"))
    return load_org("example")


def chunk(identifier: str, text: str, language: str | None = "en") -> Chunk:
    return Chunk(
        id=f"legacy-{identifier}",
        chunk_id=identifier,
        doc_id="doc-1",
        text=text,
        language=language,
        page=1,
        char_start=0,
        char_end=len(text),
    )


class RecordingProvider(FakeProvider):
    def __init__(self, failures: list[dict[str, Any] | ValueError] | None = None) -> None:
        super().__init__("test-model")
        self.failures = failures or []
        self.batches: list[list[dict[str, str]]] = []
        self.prompts: list[str] = []

    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        sources = json.loads(prompt.split("CHUNKS_JSON\n", 1)[1].split("\n\nRETRY", 1)[0])
        self.batches.append(sources)
        self.prompts.append(prompt)
        if self.calls < len(self.failures):
            failure = self.failures[self.calls]
            if isinstance(failure, ValueError):
                self.calls += 1
                raise failure
            self.response = failure
        else:
            self.response = {
                "obligations": [
                    {
                        "chunk_id": source["chunk_id"],
                        "action": "Send the report",
                        "owner_text": "",
                        "deadline_text": "",
                        "condition": None,
                        "obligation_type": "reporting",
                        "quote": source["text"],
                    }
                    for source in sources
                ],
            }
        return super().generate(prompt, schema)


def test_configured_cues_and_dates_detect_english_and_hindi_sentences(org: OrgConfig) -> None:
    english = "The office is on floor two. The supplier MUST \n file the report."
    hindi = "यह कार्यालय का विवरण है। रिपोर्ट जमा करनी होगी।"
    candidates = list(
        CandidateDetector(org).detect(
            [
                chunk("en", english),
                chunk("hi", hindi, "hi"),
                chunk("duration", "Submit the form within 10 business days."),
                chunk("hi-duration", "फ़ॉर्म ७ दिन के भीतर भेजें।", "hi"),
                chunk("dated", "Return the keys by 5 December."),
                chunk("iso", "Return the keys on 2026-12-05."),
                chunk("boring", "This parcel is ready for collection."),
                chunk("partial", "The marshal swept away the mustard seeds."),
                chunk("partial-hi", "यह आवश्यक हैसियत है।", "hi"),
            ]
        )
    )
    assert [item.chunk.chunk_id for item in candidates] == [
        "en",
        "hi",
        "duration",
        "hi-duration",
        "dated",
        "iso",
    ]
    assert candidates[0].sentences == ("The supplier MUST \n file the report.",)
    assert candidates[1].sentences == ("रिपोर्ट जमा करनी होगी।",)


def test_language_can_be_added_in_config_without_code_changes(org: OrgConfig) -> None:
    org.languages.append("fr")
    org.obligation_detection["fr"] = ObligationDetectionRules(cues=["est tenu de"])
    detector = CandidateDetector(org)
    text = "Le titulaire est tenu de notifier son client."
    candidates = list(
        detector.detect(
            [
                chunk("configured", text, "fr-CA"),
                chunk("unknown", text, None),
                chunk("other", text, "en"),
            ]
        )
    )
    assert [item.chunk.chunk_id for item in candidates] == ["configured", "unknown"]


def test_non_candidates_cost_no_calls_and_mixed_documents_are_rejected(
    org: OrgConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = RecordingProvider()
    extractor = ObligationExtractor(CandidateDetector(org), provider, ExtractionConfig())
    with caplog.at_level(logging.INFO, logger="app.extraction.extractor"):
        assert extractor.extract([chunk("boring", "This is informational.")]) == []
    assert "skipped_non_candidates=1" in caplog.text
    assert provider.calls == 0
    with pytest.raises(ValueError, match="single document"):
        extractor.extract(
            [
                chunk("first", "We must file."),
                chunk("second", "We must file.").model_copy(update={"doc_id": "another"}),
            ]
        )
    assert provider.calls == 0


def test_batching_cap_and_duplicate_chunks_preserve_input_order(
    org: OrgConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = RecordingProvider()
    config = ExtractionConfig(batch_size=2, max_chunks_per_document=3)
    extractor = ObligationExtractor(CandidateDetector(org), provider, config)
    chunks = [chunk(str(index), f"Party {index} must report.") for index in range(7)]
    with caplog.at_level(logging.INFO, logger="app.extraction.extractor"):
        result = extractor.extract([chunk("boring", "Informational."), chunks[0], *chunks])
    assert [item.chunk_id for item in result] == ["0", "1", "2"]
    assert [[item["chunk_id"] for item in batch] for batch in provider.batches] == [
        ["0", "1"],
        ["2"],
    ]
    assert "skipped_non_candidates=1 selected=3 skipped_limit=4" in caplog.text
    assert provider.calls == 2


def test_retries_invalid_json_and_invalid_schema_then_caches_valid_batch(
    org: OrgConfig,
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(
        [json.JSONDecodeError("invalid JSON", "{", 1), {"obligations": "bad"}]
    )
    config = ExtractionConfig(batch_size=2, max_chunks_per_document=1, max_retries=2)
    extractor = ObligationExtractor(CandidateDetector(org), provider, config, DiskCache(tmp_path))
    chunks = [chunk("hi", "यह काम करना होगा।", "hi"), chunk("en", "We must file.")]
    [result] = extractor.extract(chunks)
    assert result.quote == chunks[0].text
    assert len(set(provider.prompts)) == 3
    assert provider.batches == [[{"chunk_id": "hi", "text": chunks[0].text}]] * 3
    assert extractor.extract(chunks) == [result]
    assert provider.calls == 3
    [cached] = list(tmp_path.glob("*.json"))
    assert json.loads(cached.read_text()) == {"obligations": [result.model_dump(mode="json")]}


def test_bad_batch_is_never_cached(org: OrgConfig, tmp_path: Path) -> None:
    provider = RecordingProvider([{"obligations": None}, {"obligations": None}])
    extractor = ObligationExtractor(
        CandidateDetector(org), provider, ExtractionConfig(max_retries=1), DiskCache(tmp_path)
    )
    with pytest.raises(ExtractionError, match="after 2"):
        extractor.extract([chunk("en", "We must file.")])
    assert provider.calls == 2
    assert list(tmp_path.iterdir()) == []


def test_factory_config_and_zero_quota(org: OrgConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EXTRACTION_BATCH_SIZE", "3")
    monkeypatch.setenv("EXTRACTION_MAX_CHUNKS_PER_DOCUMENT", "0")
    extractor = extractor_from_env(org)
    assert extractor.config.batch_size == 3
    assert extractor.extract([chunk("en", "We must file.")]) == []
    assert isinstance(extractor.provider, FakeProvider)
    assert extractor.provider.calls == 0
