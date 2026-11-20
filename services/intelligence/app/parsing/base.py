"""Common parser interface and contract helpers."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any

from intellidocs_intelligence.models import DocumentStructure, Page, ParsedDocument, ParseResult


class DocumentParser(ABC):
    @abstractmethod
    def parse(self, file_path: Path, languages: Sequence[str]) -> ParseResult:
        """Parse a local PDF with the organization's configured language candidates."""


def dependency(name: str, extra: str) -> Any:
    try:
        return import_module(name)
    except ImportError as error:
        raise RuntimeError(
            f"Install the intelligence service [{extra}] extra to use {name}"
        ) from error


def result_for(
    path: Path,
    pages: list[Page],
    name: str,
    version: str,
    warnings: list[str],
    *,
    sizes: dict[int, tuple[float, float]] | None = None,
    languages: Sequence[str] = (),
    structure: DocumentStructure | None = None,
) -> ParseResult:
    if sizes is not None:
        from .structure import preserve_structure

        pages = preserve_structure(pages, sizes, languages, structure)
    return ParseResult(
        document=ParsedDocument(
            id=sha256(path.read_bytes()).hexdigest(),
            filename=path.name,
            mime_type="application/pdf",
            source_uri=path.resolve().as_uri(),
            status="ready",
            pages=pages,
        ),
        parser_name=name,
        parser_version=version,
        warnings=warnings,
    )


def bounded_bbox(box: Sequence[float], width: float, height: float) -> list[float]:
    x0, y0, x1, y1 = box
    return [
        min(width, max(0.0, min(x0, x1))),
        min(height, max(0.0, min(y0, y1))),
        min(width, max(0.0, max(x0, x1))),
        min(height, max(0.0, max(y0, y1))),
    ]
