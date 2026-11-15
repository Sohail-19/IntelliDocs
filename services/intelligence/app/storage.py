"""Storage boundary; parsers receive a local path, including for future remote stores."""

import json
import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from intellidocs_intelligence.models import ParseResult


class Storage(ABC):
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
