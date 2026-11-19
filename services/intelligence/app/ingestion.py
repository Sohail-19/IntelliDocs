"""Validated, local ingestion with recursive attachment persistence."""

import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from intellidocs_intelligence.models import OrgConfig, ParseResult, UploadMetadata

from .parsing.base import dependency
from .parsing.factory import parser_from_config
from .parsing.formats import (
    MIME_TYPES,
    email_content,
    logical_result,
    parse_non_email,
    validate_file,
)
from .storage import LocalStorage, Storage


def upload_limit() -> int:
    limit = int(os.environ.get("MAX_UPLOAD_BYTES", "20971520"))
    if limit <= 0:
        raise RuntimeError("MAX_UPLOAD_BYTES must be positive")
    return limit


def select_org_id(org_id: str | None) -> str:
    selected = org_id or os.environ.get("DEFAULT_ORG_ID")
    if not selected or not re.fullmatch(r"[A-Za-z0-9_-]+", selected):
        raise HTTPException(400, "A valid org_id or DEFAULT_ORG_ID is required")
    return selected


def load_org(org_id: str | None) -> OrgConfig:
    selected = select_org_id(org_id)
    root = Path(
        os.environ.get("ORG_CONFIG_DIR", Path(__file__).resolve().parents[3] / "config/orgs")
    )
    path = root / f"{selected}.yaml"
    if not path.is_file():
        raise HTTPException(404, "Organization config not found")
    yaml = dependency("yaml", "formats")
    return OrgConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def storage_from_env() -> Storage:
    root = os.environ.get("STORAGE_DIR")
    if not root:
        raise RuntimeError("STORAGE_DIR is required")
    return LocalStorage(Path(root))


class IngestionService:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def ingest(
        self,
        upload: UploadFile,
        config: OrgConfig,
        metadata: UploadMetadata | None = None,
        *,
        org_id: str | None = None,
    ) -> ParseResult:
        values = (config.upload_defaults or UploadMetadata()).model_dump(exclude_none=True)
        values.update((metadata or UploadMetadata()).model_dump(exclude_none=True))
        selected = UploadMetadata.model_validate(values)
        if (
            selected.department
            and config.departments
            and selected.department not in config.departments
        ):
            raise HTTPException(400, "department is not allowed by org config")
        if (
            selected.doc_type
            and config.document_types
            and selected.doc_type not in config.document_types
        ):
            raise HTTPException(400, "doc_type is not allowed by org config")
        limit = upload_limit()
        if upload.size is not None and upload.size > limit:
            raise HTTPException(413, "File exceeds MAX_UPLOAD_BYTES")
        content = upload.file.read(limit + 1)
        if len(content) > limit:
            raise HTTPException(413, "File exceeds MAX_UPLOAD_BYTES")
        parser = parser_from_config(config)
        created: list[str] = []
        budget = [limit]

        def store(
            filename: str, data: bytes, parent: str | None = None, depth: int = 0
        ) -> ParseResult:
            filename = Path(filename.replace("\\", "/")).name
            if not filename or "\x00" in filename:
                raise HTTPException(400, "Invalid filename")
            if depth > 5 or len(created) >= 100:
                raise HTTPException(413, "Email attachment nesting/count limit exceeded")
            budget[0] -= len(data)
            if budget[0] < 0:
                raise HTTPException(413, "Upload and decoded attachments exceed MAX_UPLOAD_BYTES")
            try:
                mime_type = validate_file(filename, data, limit)
            except ValueError as error:
                raise HTTPException(415, str(error)) from error
            identifier = str(uuid4())
            path = self.storage.save(identifier, filename, data)
            created.append(identifier)
            if path.suffix in {".eml", ".msg"}:
                text, attachments = email_content(path)
                result = logical_result(path, text.splitlines(), config.languages)
                result.attachments = []
                for attachment in attachments:
                    if Path(attachment.filename).suffix.lower() not in MIME_TYPES:
                        result.warnings.append(
                            f"Unsupported attachment skipped: {attachment.filename}"
                        )
                        continue
                    result.attachments.append(
                        store(attachment.filename, attachment.content, identifier, depth + 1)
                    )
            else:
                result = parse_non_email(path, config.languages, parser, limit)
            result.document.id = identifier
            result.document.filename = filename
            result.document.mime_type = mime_type
            result.document.source_uri = self.storage.source_uri(identifier, filename)
            result.document.parent_document_id = parent
            result.document.content_hash = hashlib.sha256(data).hexdigest()
            result.document.department = selected.department
            result.document.doc_type = selected.doc_type
            result.document.doc_date = selected.doc_date
            result.org_id = org_id
            self.storage.save_result(identifier, result)
            return result

        try:
            return store(upload.filename or "", content)
        except Exception:
            for identifier in reversed(created):
                self.storage.delete(identifier)
            raise
