"""Deployment settings and durable, atomic demo admission controls."""

import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

actor_id: ContextVar[str | None] = ContextVar("actor_id", default=None)
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def flag(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = env.get(key, str(default).lower())
    if value not in {"true", "false"}:
        raise ValueError(f"Invalid {key}")
    return value == "true"


def integer(env: Mapping[str, str], key: str, default: int, minimum: int = 1) -> int:
    value = env.get(key, str(default))
    if not re.fullmatch(r"[0-9]{1,10}", value) or not minimum <= int(value) <= 2147483647:
        raise ValueError(f"Invalid {key}")
    return int(value)


def strong_secret(key: str) -> bool:
    return (
        len(key.encode()) >= 32
        and len(set(key)) >= 10
        and not re.search(
            r"your[_ -]|placeholder|change.?me|replace.?me|example|default|development", key, re.I
        )
    )


@dataclass(frozen=True)
class Runtime:
    internal_key: str
    llm_enabled: bool
    demo: bool
    llm_per_user: int
    llm_global: int
    max_pages: int
    max_upload_bytes: int
    max_json_bytes: int
    retention_days: int
    sweep_seconds: int
    root: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Runtime":
        env = os.environ if env is None else env
        profile = env.get("INTELLIDOCS_ENV", "development")
        if profile not in {"development", "production", "test"}:
            raise ValueError("Invalid INTELLIDOCS_ENV")
        demo = flag(env, "DEMO_MODE", False)
        key = env.get("INTELLIDOCS_INTERNAL_KEY", "")
        if (profile == "production" or demo or key) and not strong_secret(key):
            raise ValueError("INTELLIDOCS_INTERNAL_KEY requires a strong shared value")
        root = env.get("STORAGE_DIR")
        if demo and not root:
            raise ValueError("STORAGE_DIR must be a persistent volume in demo mode")
        return cls(
            key,
            flag(env, "LLM_ENABLED", True),
            demo,
            integer(env, "DEMO_LLM_CALLS_PER_USER_DAY", 20, 0) if demo else 0,
            integer(env, "DEMO_LLM_CALLS_GLOBAL_DAY", 100, 0) if demo else 0,
            integer(env, "MAX_UPLOAD_PAGES", 25),
            integer(env, "MAX_UPLOAD_BYTES", 20_971_520),
            integer(env, "INTELLIGENCE_MAX_JSON_BYTES", 524_288),
            integer(env, "DEMO_RETENTION_DAYS", 7) if demo else 0,
            integer(env, "DEMO_SWEEP_INTERVAL_SECONDS", 3600) if demo else 0,
            Path(root or "data/uploads").resolve(),
        )


class FeatureUnavailable(RuntimeError):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def require_llm(runtime: Runtime) -> None:
    if not runtime.llm_enabled:
        raise FeatureUnavailable("llm_disabled", 503, "LLM features are disabled by the operator.")


class LLMQuota:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage (day TEXT, scope TEXT, used INTEGER NOT NULL, "
                "PRIMARY KEY (day, scope)) WITHOUT ROWID"
            )

    def reserve(self, runtime: Runtime, actor: str | None, now: datetime | None = None) -> None:
        today = (now or datetime.now(UTC)).astimezone(UTC).date()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for scope, maximum in (
                ("global", runtime.llm_global),
                (f"actor:{actor or 'system'}", runtime.llm_per_user),
            ):
                if (
                    maximum == 0
                    or db.execute(
                        "INSERT INTO usage (day, scope, used) VALUES (?, ?, 1) "
                        "ON CONFLICT (day, scope) DO UPDATE SET used=usage.used+1 "
                        "WHERE usage.used < ?",
                        (today.isoformat(), scope, maximum),
                    ).rowcount
                    != 1
                ):
                    raise FeatureUnavailable(
                        "llm_quota",
                        429,
                        "Daily demo LLM limit reached. Try again after midnight UTC.",
                    )
            db.execute(
                "DELETE FROM usage WHERE day < ?", ((today - timedelta(days=2)).isoformat(),)
            )


@lru_cache(maxsize=8)
def llm_quota(root: Path) -> LLMQuota:
    return LLMQuota(root / ".demo-llm.sqlite3")


def cache_directory(settings: Runtime | None = None) -> Path:
    runtime = settings or Runtime.from_env()
    if runtime.demo:
        return runtime.root / "demo-cache"
    override = os.environ.get("LLM_CACHE_DIR")
    return Path(override) if override else runtime.root / "llm-cache"
