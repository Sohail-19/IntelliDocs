"""Construct parsers using the organization configuration as the authority."""

import os
from pathlib import Path

from intellidocs_intelligence.models import OrgConfig

from .base import DocumentParser
from .docling_parser import DoclingParser
from .languages import language_codes
from .router import ParsingRouter
from .tesseract_parser import TesseractParser


def parser_from_config(config: OrgConfig, *, artifacts_path: Path | None = None) -> DocumentParser:
    if config.ocr_engine not in {"docling", "tesseract"}:
        raise ValueError(f"Unsupported OCR engine: {config.ocr_engine!r}; use docling or tesseract")
    language_codes(config.languages)
    if config.ocr_engine == "tesseract":
        return TesseractParser()
    if artifacts_path is None and os.environ.get("DOCLING_ARTIFACTS_PATH"):
        artifacts_path = Path(os.environ["DOCLING_ARTIFACTS_PATH"])
    return ParsingRouter(DoclingParser(artifacts_path), TesseractParser())
