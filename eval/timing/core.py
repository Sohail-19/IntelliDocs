"""Small Core HTTP client for pilot setup and request timing."""

import json
from dataclasses import dataclass
from http.client import HTTPException
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    # Credentials and bearer tokens are scoped to the configured Core origin.
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class Exchange:
    seconds: float
    status: int | None
    body: bytes
    error: str | None = None

    def json(self) -> dict[str, Any]:
        data = json.loads(self.body)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object from Core")
        return data


class Core:
    def __init__(self, url: str, timeout: float) -> None:
        parsed = urlsplit(url)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme != "https"
                and (
                    parsed.scheme != "http"
                    or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                )
            )
        ):
            raise ValueError("Core URL must be an HTTPS origin or loopback HTTP origin")
        self.url, self.timeout = url.rstrip("/"), timeout
        self.opener = build_opener(NoRedirect())

    def request(
        self,
        path: str,
        *,
        data: bytes | None = None,
        token: str | None = None,
        content_type: str = "application/json",
    ) -> Exchange:
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = content_type
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.url + path, data=data, headers=headers)
        start = perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                return Exchange(perf_counter() - start, response.status, body)
        except HTTPError as error:
            with error:
                try:
                    body = error.read()
                except (OSError, HTTPException):
                    body = b""
                return Exchange(perf_counter() - start, error.code, body, "http_error")
        except (OSError, HTTPException) as error:
            reason = error.reason if isinstance(error, URLError) else error
            code = "timeout" if isinstance(reason, TimeoutError) else "connection_error"
            return Exchange(perf_counter() - start, None, b"", code)

    def post(self, path: str, data: dict[str, Any], token: str | None = None) -> Exchange:
        return self.request(path, data=json.dumps(data).encode(), token=token)

    def login(self, org_id: str, email: str, password: str) -> str:
        result = self.post(
            "/api/auth/login", {"org_id": org_id, "email": email, "password": password}
        )
        if result.status != 200:
            raise ValueError(f"Login failed for {org_id} ({result.status or result.error})")
        token = result.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError(f"No access token returned for {org_id}")
        return token
