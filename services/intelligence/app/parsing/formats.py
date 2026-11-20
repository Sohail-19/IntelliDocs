"""Local adapters for image, DOCX, and exported email content."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from intellidocs_intelligence.models import (
    DocumentStructure,
    Page,
    ParseResult,
    TableBlock,
    TextBlock,
)

from .base import DocumentParser, dependency, result_for

_FORMAT_NAME = "document-upload-formats.v1.json"
_FORMAT_PATH = Path(__file__).resolve().parents[4] / "contracts" / _FORMAT_NAME
if not _FORMAT_PATH.is_file():
    _FORMAT_PATH = Path("/contracts") / _FORMAT_NAME
_FORMATS: dict[str, dict[str, str]] = json.loads(_FORMAT_PATH.read_text(encoding="utf-8"))
MIME_TYPES = {suffix: entry["mime_type"] for suffix, entry in _FORMATS.items()}
_SIGNATURES = {suffix: bytes.fromhex(entry["signature"]) for suffix, entry in _FORMATS.items()}


def validate_file(filename: str, content: bytes, max_bytes: int) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in MIME_TYPES:
        raise ValueError(f"Unsupported file type {suffix!r}")
    if not content or not content.startswith(_SIGNATURES[suffix]):
        raise ValueError("File contents do not match the filename type")
    return MIME_TYPES[suffix]


@dataclass
class Attachment:
    filename: str
    content: bytes


class HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def email_content(path: Path) -> tuple[str, list[Attachment]]:
    attachments: list[Attachment] = []
    if path.suffix == ".msg":
        module = dependency("extract_msg", "formats")
        with module.openMsg(str(path)) as message:
            text = f"Subject: {message.subject or ''}\nFrom: {message.sender or ''}\n\n".replace(
                "\x00", ""
            )
            text += (message.body or "").strip("\x00")
            for attachment in message.attachments:
                data = attachment.data
                if not isinstance(data, bytes) and hasattr(data, "exportBytes"):
                    data = data.exportBytes()
                if not isinstance(data, bytes):
                    raise ValueError("Unsupported embedded MSG attachment encoding")
                attachments.append(Attachment(attachment.getFilename().rstrip("\x00"), data))
            return text, attachments
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    if not any(message.get(key) for key in ("From", "Subject", "To", "Date")):
        raise ValueError("Invalid email export: message headers are missing")
    body = message.get_body(preferencelist=("plain", "html"))
    text = str(body.get_content()) if body else ""
    if body and body.get_content_type() == "text/html":
        parser = HTMLText()
        parser.feed(text)
        text = "\n".join(parser.parts)

    def collect(part: Message) -> None:
        if part.get_content_type() == "message/rfc822":
            nested = part.get_payload()
            if isinstance(nested, list) and nested and isinstance(nested[0], Message):
                attachments.append(
                    Attachment(part.get_filename() or "message.eml", nested[0].as_bytes())
                )
                return
        if part.get_filename() or part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                attachments.append(Attachment(part.get_filename() or "attachment", payload))
                return
        if part.is_multipart():
            children = part.get_payload()
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, Message):
                        collect(child)

    collect(message)
    return (
        f"Subject: {message.get('Subject', '')}\nFrom: {message.get('From', '')}\n\n{text}",
        attachments,
    )


def logical_result(
    path: Path,
    lines: Sequence[str | list[list[str]] | TextBlock],
    languages: Sequence[str],
    *,
    structure: DocumentStructure | None = None,
) -> ParseResult:
    pages: list[Page] = []
    for start in range(0, max(1, len(lines)), 35):
        blocks: list[TextBlock | TableBlock] = []
        for order, line in enumerate(lines[start : start + 35]):
            bbox = [36.0, 40.0 + order * 20, 576.0, 58.0 + order * 20]
            if isinstance(line, TextBlock):
                blocks.append(line.model_copy(update={"bbox": bbox, "reading_order": order}))
            elif isinstance(line, list):
                blocks.append(TableBlock(text="", rows=line, bbox=bbox, reading_order=order))
            else:
                blocks.append(
                    TextBlock(type="paragraph", text=line, bbox=bbox, reading_order=order)
                )
        pages.append(Page(page=len(pages) + 1, language=languages[0], blocks=blocks))
    return result_for(
        path,
        pages,
        "local-formats",
        "1",
        ["Logical pages and coordinates; physical pagination is unavailable."],
        sizes={page.page: (612, 792) for page in pages},
        languages=languages,
        structure=structure,
    )


def parse_non_email(
    path: Path,
    languages: Sequence[str],
    parser: DocumentParser,
    max_bytes: int,
    *,
    structure: DocumentStructure | None = None,
) -> ParseResult:
    if path.suffix == ".pdf":
        return parser.parse(path, languages)
    if path.suffix in {".png", ".jpg", ".jpeg"}:
        image_module = dependency("PIL.Image", "tesseract")
        with TemporaryDirectory(prefix="intellidocs-image-") as directory:
            pdf = Path(directory) / "image.pdf"
            with image_module.open(path) as image:
                image.convert("RGB").save(pdf, "PDF", resolution=72)
            return parser.parse(pdf, languages)
    with ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > max_bytes:
            raise ValueError("DOCX expanded contents exceed upload limit")
        if "word/document.xml" not in archive.namelist():
            raise ValueError("Invalid DOCX archive")
    module = dependency("docx", "formats")
    document = module.Document(path)
    lines: list[str | list[list[str]] | TextBlock] = []
    for item in document.iter_inner_content():
        if hasattr(item, "rows"):
            lines.append([[cell.text for cell in row.cells] for row in item.rows])
        elif item.text.strip():
            style = item.style.style_id if item.style else ""
            if style.startswith("Heading") and style[7:].isdigit():
                lines.append(
                    TextBlock(
                        type="heading",
                        text=item.text,
                        bbox=[0, 0, 0, 0],
                        reading_order=0,
                        heading_level=int(style[7:]),
                    )
                )
            else:
                lines.append(item.text)
    return logical_result(path, lines, languages, structure=structure)
