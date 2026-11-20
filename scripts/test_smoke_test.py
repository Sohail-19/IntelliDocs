import json
from email.message import Message
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import smoke_test


def test_hosted_smoke_verifies_cookie_proxy_upload_and_filtered_search(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    intelligence, core, web = (
        "https://intelligence.example.test",
        "https://core.example.test",
        "https://web.example.test",
    )
    doc = "96bac0ba-0297-4c0a-a1d8-441184abb049"
    password = "test-value-not-for-logs"
    monkeypatch.setenv("SMOKE_ORG_ID", "example")
    monkeypatch.setenv("SMOKE_EMAIL", "pilot@example.test")
    monkeypatch.setenv("SMOKE_PASSWORD", password)
    calls: list[tuple[str, str]] = []

    def reply(status: int, body: object, headers: Message | None = None) -> smoke_test.Reply:
        return smoke_test.Reply(status, json.dumps(body).encode(), headers or Message())

    def send(base: str, path: str, **options: object) -> smoke_test.Reply:
        calls.append((base, path))
        if base in {intelligence, core}:
            return reply(200, {"status": "ready" if path == "/ready" else "ok"})
        assert base == web
        if path == "/login":
            return smoke_test.Reply(200, b'<div id="root"></div>', Message())
        if path == "/api/auth/login":
            assert options["browser_origin"] == web
            request = options["body"]
            assert isinstance(request, bytes) and json.loads(request)["password"] == password
            headers = Message()
            headers["Set-Cookie"] = (
                "__Host-intellidocs-refresh=fixture; Secure; HttpOnly; SameSite=Strict; Path=/"
            )
            return reply(200, {"access_token": "initial"}, headers)
        if path == "/api/auth/refresh":
            assert options["cookie"] == "__Host-intellidocs-refresh=fixture"
            return reply(200, {"access_token": "rotated"})
        assert options["token"] == "rotated"
        if path == "/v1/upload":
            blob = options["body"]
            assert isinstance(blob, bytes) and smoke_test.FIXTURE.read_bytes() in blob
            return reply(202, {"document_id": doc})
        if path == f"/api/documents/{doc}":
            return reply(200, {"document": {"id": doc, "status": "indexed"}})
        parsed = urlsplit(path)
        assert parsed.path == "/api/search" and parse_qs(parsed.query)["doc_id"] == [doc]
        return reply(200, {"results": [{"chunk": {"doc_id": doc}}]})

    monkeypatch.setattr(smoke_test, "send", send)
    smoke_test.smoke(intelligence, core, web, 1)
    assert sum(path == "/v1/upload" for _, path in calls) == 1
    assert sum(path.startswith("/api/search?") for _, path in calls) == 1
    assert password not in capsys.readouterr().out
