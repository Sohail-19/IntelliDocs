import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from intellidocs_intelligence import http_runtime
from intellidocs_intelligence.cache import CachedLLMProvider, DiskCache
from intellidocs_intelligence.http_runtime import SafeFormatter
from intellidocs_intelligence.llm import FakeProvider, MeteredProvider, provider_from_env
from intellidocs_intelligence.main import app, get_answer_factory
from intellidocs_intelligence.models import Answer, AnswerRequest
from intellidocs_intelligence.runtime import FeatureUnavailable, LLMQuota, Runtime, actor_id


def test_llm_quota_is_atomic_per_actor_and_global(tmp_path: Path) -> None:
    ledger = LLMQuota(tmp_path / "usage.sqlite3")
    settings = replace(Runtime.from_env({}), llm_per_user=1, llm_global=3)
    day = datetime(2026, 11, 20, tzinfo=UTC)

    def call(actor: str) -> bool:
        try:
            ledger.reserve(settings, actor, day)
        except FeatureUnavailable as error:
            assert error.code == "llm_quota"
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(call, ["u1"] * 12)) == 1
        assert sum(workers.map(call, ["u2", "u3", "u4", "u5"])) == 2
    persisted = LLMQuota(ledger.path)
    with pytest.raises(FeatureUnavailable, match="Daily demo LLM limit reached"):
        persisted.reserve(settings, "u6", day)
    persisted.reserve(settings, "u1", day + timedelta(days=1))


def test_meter_charges_provider_invocations_and_cache_hits_are_free(tmp_path: Path) -> None:
    settings = replace(Runtime.from_env({}), root=tmp_path, llm_per_user=1, llm_global=4)
    delegate = FakeProvider("fixture", {"value": "ok"})
    provider = CachedLLMProvider(MeteredProvider(delegate, settings), DiskCache(tmp_path / "cache"))
    token = actor_id.set("7")
    try:
        assert provider.generate("first", {}) == {"value": "ok"}
        assert provider.generate("first", {}) == {"value": "ok"}
        with pytest.raises(FeatureUnavailable, match="Daily demo LLM limit reached"):
            provider.generate("second", {})
        assert delegate.calls == 1
    finally:
        actor_id.reset(token)


def test_missing_internal_key_prevents_production_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTELLIDOCS_ENV", "production")
    monkeypatch.delenv("INTELLIDOCS_INTERNAL_KEY", raising=False)
    with pytest.raises(ValueError, match="INTELLIDOCS_INTERNAL_KEY"), TestClient(app):
        pytest.fail("Startup accepted a missing key")


def test_kill_switch_private_transport_and_safe_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    planted = "private-customer-document-and-credentials"
    internal = token_hex(32)
    monkeypatch.setenv("INTELLIDOCS_INTERNAL_KEY", internal)
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_ENABLED", "false")
    key = "4f6437ac-42f1-40af-b0e7-57c6de191d8a"
    with TestClient(app) as client:
        denied = client.post(f"/v1/answer?query={planted}", headers={"X-Request-ID": planted})
        assert denied.status_code == 401
        for path, body in (
            ("answer", {"query": planted, "top_k": 1, "language": "en"}),
            ("summarize", {"doc_id": "doc", "department": "finance", "language": "en"}),
            ("extract-obligations", {"doc_id": "doc"}),
            ("detect-relations", {"doc_id": "doc"}),
        ):
            response = client.post(
                f"/v1/{path}",
                json=body,
                headers={
                    "X-IntelliDocs-Internal-Key": internal,
                    "X-Request-ID": key,
                    "Authorization": planted,
                },
            )
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "llm_disabled"
            assert response.headers["X-Request-ID"] == key
        assert client.get("/health").status_code == 200
    output = capsys.readouterr().out
    assert planted not in output and internal not in output
    assert {json.loads(line)["status"] for line in output.splitlines()} == {200, 401, 503}
    record = logging.LogRecord(
        "test", logging.ERROR, "test", 0, "Provider says %s", (planted,), None
    )
    record.__dict__.update(intellidocs_event="obligation_candidates", scanned=3, evidence=planted)
    formatted = SafeFormatter().format(record)
    assert planted not in formatted
    assert json.loads(formatted)["scanned"] == 3


def test_readiness_probes_qdrant_only(monkeypatch: pytest.MonkeyPatch) -> None:
    qdrant = Mock()
    monkeypatch.setattr(http_runtime, "QdrantClient", Mock(return_value=qdrant))
    monkeypatch.setenv("QDRANT_URL", "http://test.invalid")
    monkeypatch.setenv("QDRANT_COLLECTION", "fixture")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 503
        monkeypatch.setenv("LLM_API_KEY", "local-test-credential")
        assert client.get("/ready").json()["llm"] == "key_present"
        qdrant.get_collections.side_effect = OSError("unavailable")
        assert client.get("/ready").status_code == 503


def test_pdf_page_limit_rejects_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_UPLOAD_PAGES", "1")
    parse = Mock(side_effect=AssertionError("OCR should not run"))
    monkeypatch.setattr("app.ingestion.parse_non_email", parse)
    fixture = Path(__file__).with_name("fixtures") / "structured-scanned.pdf"
    with TestClient(app) as client:
        response = client.post(
            "/v1/ingest",
            data={"org_id": "example"},
            files={"file": (fixture.name, fixture.read_bytes(), "application/pdf")},
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "upload_pages"
    parse.assert_not_called()


def test_json_byte_limit_also_applies_to_streamed_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTELLIGENCE_MAX_JSON_BYTES", "128")
    with TestClient(app) as client:
        payload = b'{"query":"' + b"x" * 256 + b'","top_k":1}'
        assert (
            client.post(
                "/v1/search",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                },
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/v1/search",
                content=iter([payload[:100], payload[100:]]),
                headers={
                    "Content-Type": "application/json",
                },
            ).status_code
            == 413
        )


def test_http_routes_apply_actor_and_global_provider_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = token_hex(32)
    monkeypatch.setenv("INTELLIDOCS_INTERNAL_KEY", key)
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_LLM_CALLS_PER_USER_DAY", "1")
    monkeypatch.setenv("DEMO_LLM_CALLS_GLOBAL_DAY", "2")

    class FixtureAnswer:
        def answer(self, _request: AnswerRequest) -> Answer:
            provider = provider_from_env()
            provider.generate("a prompt", {})
            return Answer(
                answer_text="",
                claims=[],
                abstained=True,
                abstain_reason="Fixture",
                retrieved_chunk_ids=[],
                model=provider.model,
                verifier_version="1",
            )

    @contextmanager
    def factory() -> Iterator[FixtureAnswer]:
        yield FixtureAnswer()

    monkeypatch.setitem(app.dependency_overrides, get_answer_factory, lambda: factory)
    with TestClient(app) as client:
        for actor, status in (("1", 200), ("1", 429), ("2", 200), ("3", 429)):
            response = client.post(
                "/v1/answer",
                json={"query": "q", "top_k": 1, "language": "en"},
                headers={"X-IntelliDocs-Internal-Key": key, "X-IntelliDocs-Actor": actor},
            )
            assert response.status_code == status
            if status == 429:
                assert response.json()["detail"]["code"] == "llm_quota"
