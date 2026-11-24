"""Check an IntelliDocs deployment using one synthetic upload and one search."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from functools import partial
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

FIXTURE = Path(__file__).resolve().parents[1] / "services/intelligence/tests/fixtures/digital.pdf"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        # Do not forward passwords, cookies, or bearer tokens to a redirect destination.
        return None


OPENER = build_opener(NoRedirect())


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    headers: Message

    def json(self) -> dict[str, Any]:
        result = json.loads(self.body)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result


def origin(raw: str) -> str:
    url = urlsplit(raw)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or (
            url.scheme != "https"
            and (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"})
        )
    ):
        raise argparse.ArgumentTypeError("Use a bare HTTPS origin or a loopback HTTP origin")
    return raw.rstrip("/")


def send(
    base: str,
    path: str,
    *,
    body: bytes | None = None,
    token: str | None = None,
    browser_origin: str | None = None,
    cookie: str | None = None,
    content_type: str = "application/json",
    timeout: float = 15,
) -> Reply:
    headers = {"Accept": "application/json, text/html"}
    if body is not None:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if browser_origin:
        headers["Origin"] = browser_origin
    if cookie:
        headers["Cookie"] = cookie
    request = Request(base + path, data=body, headers=headers)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return Reply(response.status, response.read(), response.headers)
    except HTTPError as error:
        with error:
            return Reply(error.code, b"", error.headers)


def wait_for(label: str, check: Callable[[float], bool], seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            if check(min(15, remaining)):
                print(f"OK {label}", flush=True)
                return
        except (OSError, HTTPException, ValueError):
            pass
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    raise ValueError(f"Timed out waiting for {label}")


def check_status(base: str, path: str, expected: str, timeout: float) -> bool:
    response = send(base, path, timeout=timeout)
    return response.status == 200 and response.json().get("status") == expected


def access_token(response: Reply, operation: str) -> str:
    if response.status != 200:
        raise ValueError(f"{operation} returned HTTP {response.status}")
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError(f"{operation} did not return an access token")
    return token


def smoke(intelligence: str, core: str, web: str, wait: int) -> None:
    credentials = {
        field: os.environ.get(f"SMOKE_{field.upper()}", "")
        for field in ("org_id", "email", "password")
    }
    if not all(credentials.values()):
        raise ValueError("Set SMOKE_ORG_ID, SMOKE_EMAIL, and SMOKE_PASSWORD")

    for service, url in (("Intelligence", intelligence), ("Core", core)):
        wait_for(f"{service} health", partial(check_status, url, "/health", "ok"), wait)
        wait_for(
            f"{service} readiness",
            partial(check_status, url, "/ready", "ready"),
            wait,
        )

    def web_login(timeout: float) -> bool:
        response = send(web, "/login", timeout=timeout)
        return response.status == 200 and b'id="root"' in response.body

    wait_for("web login page", web_login, wait)
    # Production browsers use the web proxy. A local Compose web image uses Core directly.
    deployed = urlsplit(web).scheme == "https"
    api = web if deployed else core
    signed_in = send(
        api, "/api/auth/login", body=json.dumps(credentials).encode(), browser_origin=web
    )
    token = access_token(signed_in, "Demo login")
    if deployed:
        refresh = next(
            (
                value
                for value in signed_in.headers.get_all("Set-Cookie", [])
                if value.startswith("__Host-intellidocs-refresh=")
            ),
            "",
        )
        attributes = {part.strip().lower() for part in refresh.split(";")[1:]}
        if (
            not {"secure", "httponly", "samesite=strict", "path=/"} <= attributes
            or "refresh_token" in signed_in.json()
        ):
            raise ValueError("Web proxy did not deliver the secured refresh cookie")
        response = send(
            api,
            "/api/auth/refresh",
            body=b"{}",
            browser_origin=web,
            cookie=refresh.split(";", 1)[0],
        )
        token = access_token(response, "Cookie refresh")
    print("OK demo login", flush=True)

    boundary = secrets.token_hex(16)
    payload = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{FIXTURE.name}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + FIXTURE.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    uploaded = send(
        api,
        "/v1/upload",
        body=payload,
        token=token,
        browser_origin=web,
        content_type=f"multipart/form-data; boundary={boundary}",
        timeout=min(125, wait),
    )
    if uploaded.status != 202:
        raise ValueError(
            f"Upload returned HTTP {uploaded.status}. Check permissions and demo quota."
        )
    doc_id = str(UUID(uploaded.json()["document_id"]))

    def indexed(timeout: float) -> bool:
        reply = send(api, f"/api/documents/{doc_id}", token=token, timeout=timeout)
        if reply.status in {401, 403}:
            raise RuntimeError(f"Cannot read the uploaded document (HTTP {reply.status})")
        if reply.status != 200:
            return False
        record = reply.json()["document"]
        if record["status"] == "failed":
            raise RuntimeError("Fixture indexing failed. Check Core and Intelligence logs.")
        return record["id"] == doc_id and record["status"] == "indexed"

    wait_for("fixture indexed and ready for search", indexed, wait)
    query = urlencode({"query": "Original digital fixture text", "doc_id": doc_id, "top_k": 3})
    search = send(api, f"/api/search?{query}", token=token, timeout=min(125, wait))
    if search.status != 200 or not any(
        row["chunk"]["doc_id"] == doc_id for row in search.json()["results"]
    ):
        raise ValueError(f"Search did not return the uploaded fixture (HTTP {search.status})")
    print(f"OK search found uploaded fixture {doc_id}", flush=True)


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    for service in ("intelligence_url", "core_url", "web_url"):
        cli.add_argument(service, type=origin)
    args = cli.parse_args()
    try:
        wait = int(os.environ.get("SMOKE_WAIT_SECONDS", "240"))
        if not 1 <= wait <= 900:
            raise ValueError("SMOKE_WAIT_SECONDS must be between 1 and 900")
        smoke(args.intelligence_url, args.core_url, args.web_url, wait)
    except (ValueError, KeyError, TypeError, RuntimeError, HTTPException, OSError) as error:
        print(f"Smoke test failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
