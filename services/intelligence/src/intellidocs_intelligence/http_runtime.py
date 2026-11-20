"""Internal HTTP identity, bounded request bodies, and safe operational events."""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from secrets import compare_digest
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from qdrant_client import QdrantClient
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .runtime import Runtime, actor_id, cache_directory, llm_quota, request_id


def log_event(event: str, **fields: object) -> None:
    print(
        json.dumps(
            {"service": "intelligence", "event": event, "request_id": request_id.get(), **fields}
        ),
        flush=True,
    )


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Keep library errors visible without their exception text, arguments or stack traces.
        data: dict[str, object] = {
            "service": "intelligence",
            "event": "runtime_log",
            "level": record.levelname,
            "request_id": request_id.get(),
        }
        if getattr(record, "intellidocs_event", None) == "obligation_candidates":
            data["event"] = "obligation_candidates"
            for key in ("scanned", "skipped_non_candidates", "selected", "skipped_limit"):
                value = getattr(record, key, None)
                if type(value) is int:
                    data[key] = value
        return json.dumps(data)


class InternalHTTP:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = Runtime.from_env()
        headers = Headers(scope=scope)
        authorized = bool(settings.internal_key) and compare_digest(
            headers.get("x-intellidocs-internal-key", "").encode(), settings.internal_key.encode()
        )
        incoming_id = headers.get("x-request-id", "")
        trusted_id = authorized and re.fullmatch(r"[a-f0-9-]{36}", incoming_id)
        id_token = request_id.set(incoming_id if trusted_id else str(uuid4()))
        actor = headers.get("x-intellidocs-actor", "")
        actor_token = actor_id.set(
            actor if authorized and re.fullmatch(r"[1-9][0-9]{0,9}", actor) else None
        )
        status = 500

        async def tracked_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = request_id.get() or ""
            await send(message)

        try:
            path = scope["path"]
            if path.startswith("/v1/") and settings.internal_key and not authorized:
                await JSONResponse({"detail": "Internal authentication required"}, 401)(
                    scope, receive, tracked_send
                )
                return
            if path == "/v1/ingest" and settings.demo and actor_id.get() is None:
                await JSONResponse({"detail": "Authenticated upload actor required"}, 403)(
                    scope, receive, tracked_send
                )
                return
            upload = path.rstrip("/") == "/v1/ingest"
            maximum = settings.max_upload_bytes + 65_536 if upload else settings.max_json_bytes
            length = headers.get("content-length", "0")
            if not re.fullmatch(r"[0-9]{1,16}", length) or int(length) > maximum:
                await JSONResponse({"detail": "Request body exceeds the size limit"}, 413)(
                    scope, receive, tracked_send
                )
                return
            seen = 0

            async def bounded_receive() -> Message:
                nonlocal seen
                message = await receive()
                if message["type"] == "http.request":
                    seen += len(message.get("body", b""))
                    if seen > maximum:
                        raise HTTPException(413, "Request body exceeds the size limit")
                return message

            await self.app(scope, bounded_receive, tracked_send)
        finally:
            method = scope["method"]
            log_event(
                "request",
                method=method
                if method in {"GET", "POST", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER",
                status=status,
            )
            actor_id.reset(actor_token)
            request_id.reset(id_token)


def prune_demo_cache(settings: Runtime) -> None:
    cutoff = time.time() - settings.retention_days * 86400
    for item in cache_directory(settings).rglob("*.json"):
        with suppress(FileNotFoundError):
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    runtime = Runtime.from_env()
    if not runtime.demo:
        yield
        return
    llm_quota(runtime.root)

    async def sweep() -> None:
        while True:
            try:
                await asyncio.to_thread(prune_demo_cache, runtime)
            except OSError:
                log_event("demo_cache_cleanup_failed")
            await asyncio.sleep(runtime.sweep_seconds)

    task = asyncio.create_task(sweep())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def readiness() -> tuple[int, dict[str, str]]:
    checks = {"qdrant": "unavailable"}
    try:
        url = os.environ.get("QDRANT_URL")
        if not url or not os.environ.get("QDRANT_COLLECTION"):
            raise ValueError("Qdrant is not configured")
        client = QdrantClient(url=url, api_key=os.environ.get("QDRANT_API_KEY") or None, timeout=2)
        try:
            client.get_collections()
            checks["qdrant"] = "ok"
        finally:
            client.close()
    except Exception:
        pass
    provider = os.environ.get("LLM_PROVIDER", "fake")
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not Runtime.from_env().llm_enabled:
        checks["llm"] = "disabled"
    elif provider == "fake":
        checks["llm"] = "not_required"
    elif provider not in {"gemini", "openai_compatible"}:
        checks["llm"] = "unavailable"
    elif not key or key.upper().startswith(("YOUR_", "PLACEHOLDER", "CHANGE_ME")):
        checks["llm"] = "missing_key"
    elif provider == "openai_compatible" and not os.environ.get("LLM_BASE_URL", "").strip():
        checks["llm"] = "unavailable"
    else:
        checks["llm"] = "key_present"
    ready = checks["qdrant"] == "ok" and checks["llm"] in {
        "disabled",
        "not_required",
        "key_present",
    }
    return (200 if ready else 503), {"status": "ready" if ready else "not_ready", **checks}
