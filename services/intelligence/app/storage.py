"""Storage boundary; parsers receive a local path, including for future remote stores."""

import json
import re
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import portalocker

from intellidocs_intelligence.models import ParseResult


class Storage(ABC):
    @abstractmethod
    def load_result(self, document_id: str) -> ParseResult:
        """Read a previously persisted parse result."""

    @abstractmethod
    def source_path(self, document_id: str, filename: str) -> Path:
        """Materialize the original file locally for content verification."""

    @abstractmethod
    def load_index_cache(self, document_id: str) -> dict[str, Any] | None:
        """Read embedding cache and last acknowledged indexing state."""

    @abstractmethod
    def save_index_cache(self, document_id: str, cache: dict[str, Any]) -> None:
        """Atomically persist cached vectors/index state."""

    @abstractmethod
    def index_lock(self, document_id: str) -> AbstractContextManager[None]:
        """Serialize index requests for one document across local processes."""

    @abstractmethod
    def save(self, document_id: str, filename: str, content: bytes) -> Path:
        """Store original bytes and return a local path for parsing."""

    @abstractmethod
    def save_result(self, document_id: str, result: ParseResult) -> None:
        """Persist the parse result alongside the source."""

    @abstractmethod
    def source_uri(self, document_id: str, filename: str) -> str:
        """Return the durable source reference, independent of its staging path."""

    @abstractmethod
    def delete(self, document_id: str) -> None:
        """Roll back a newly created document and its result."""


class LocalStorage(Storage):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _directory(self, document_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9-]{36}", document_id):
            raise ValueError("Invalid storage document ID")
        return self.root / document_id

    def save(self, document_id: str, filename: str, content: bytes) -> Path:
        directory = self._directory(document_id)
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / f"source{Path(filename).suffix.lower()}"
        try:
            path.write_bytes(content)
        except OSError:
            shutil.rmtree(directory)
            raise
        return path

    def load_result(self, document_id: str) -> ParseResult:
        result = ParseResult.model_validate_json(
            (self._directory(document_id) / "parse_result.json").read_text(encoding="utf-8"),
        )
        if result.document.id != document_id:
            raise ValueError("Stored ParseResult document ID does not match the requested ID")
        return result

    def source_path(self, document_id: str, filename: str) -> Path:
        return self._directory(document_id) / f"source{Path(filename).suffix.lower()}"

    def load_index_cache(self, document_id: str) -> dict[str, Any] | None:
        path = self._directory(document_id) / "index_cache.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def save_index_cache(self, document_id: str, cache: dict[str, Any]) -> None:
        path = self._directory(document_id) / "index_cache.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(cache, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )
        temporary.replace(path)

    @contextmanager
    def index_lock(self, document_id: str) -> Iterator[None]:
        directory = self._directory(document_id)
        if not directory.is_dir():
            raise FileNotFoundError(document_id)
        with portalocker.Lock(str(directory / ".index.lock"), timeout=30):
            yield

    def source_uri(self, document_id: str, filename: str) -> str:
        return (self._directory(document_id) / f"source{Path(filename).suffix.lower()}").as_uri()

    def save_result(self, document_id: str, result: ParseResult) -> None:
        directory = self._directory(document_id)
        temporary = directory / "parse_result.json.tmp"
        temporary.write_text(
            json.dumps(result.model_dump(mode="json", exclude_none=True), ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(directory / "parse_result.json")

    def delete(self, document_id: str) -> None:
        directory = self._directory(document_id)
        if directory.is_dir():
            shutil.rmtree(directory)
