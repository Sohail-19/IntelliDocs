"""Exercise each real format decoder and storage, with an offline PDF parser seam."""

import json
import shutil
import socket
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from app.parsing.factory import parser_from_config
from intellidocs_intelligence.main import app
from intellidocs_intelligence.models import ParseResult

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Ingestion must not call the network")

    monkeypatch.setattr(socket.socket, "connect", fail)


@pytest.mark.parametrize(
    "filename",
    ["digital.pdf", "upload.png", "upload.jpg", "upload.docx", "upload.eml", "upload.msg"],
)
def test_ingest_formats(filename: str, ingestion_setup: Path) -> None:
    response = TestClient(app).post(
        "/v1/ingest",
        files={
            "file": (filename, (FIXTURES / filename).read_bytes()),
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    result = body["parse_result"]
    schema_root = Path(__file__).resolve().parents[3] / "contracts/schemas"
    registry = Registry().with_resources(
        (path.name, Resource.from_contents(json.loads(path.read_text())))
        for path in schema_root.glob("*.json")
    )
    schema = json.loads((schema_root / "ingest-response.v1.schema.json").read_text())
    Draft202012Validator(schema, registry=registry).validate(body)
    assert result["document"]["id"] == body["document_id"]
    assert result["document"]["filename"] == filename
    assert result["document"]["status"] == "ready"
    assert result["document"]["pages"]
    directory = ingestion_setup / body["document_id"]
    assert (directory / f"source{Path(filename).suffix}").read_bytes() == (
        FIXTURES / filename
    ).read_bytes()
    assert json.loads((directory / "parse_result.json").read_text()) == result
    if filename.endswith((".eml", ".msg")):
        assert len(result["attachments"]) == 1
        child = result["attachments"][0]["document"]
        assert child["parent_document_id"] == body["document_id"]
        assert (ingestion_setup / child["id"] / "parse_result.json").is_file()
    if filename.endswith(".docx"):
        blocks = result["document"]["pages"][0]["blocks"]
        assert blocks[0]["heading_level"] == 1
        assert blocks[-1]["rows"] == [["Item", "Count"], ["Reports", "12"]]


def test_oversize_upload(ingestion_setup: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    response = TestClient(app).post("/v1/ingest", files={"file": ("large.pdf", b"%PDF-" * 5)})
    assert response.status_code == 413
    assert not ingestion_setup.exists()


@pytest.mark.parametrize("filename,content", [("bad.exe", b"data"), ("bad.pdf", b"not a pdf")])
def test_rejects_invalid_type(filename: str, content: bytes, ingestion_setup: Path) -> None:
    response = TestClient(app).post("/v1/ingest", files={"file": (filename, content)})
    assert response.status_code == 415
    assert not ingestion_setup.exists()


def test_parse_failure_rolls_back(ingestion_setup: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> ParseResult:
        raise ValueError("Broken parser input")

    monkeypatch.setattr("app.ingestion.parse_non_email", fail)
    response = TestClient(app).post(
        "/v1/ingest",
        files={
            "file": ("digital.pdf", (FIXTURES / "digital.pdf").read_bytes()),
        },
    )
    assert response.status_code == 422
    assert not list(ingestion_setup.iterdir())


def test_decoded_attachment_budget_rolls_back(
    ingestion_setup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (FIXTURES / "upload.eml").read_bytes()
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(len(content) + 1))
    response = TestClient(app).post("/v1/ingest", files={"file": ("mail.eml", content)})
    assert response.status_code == 413
    assert not list(ingestion_setup.iterdir())


def test_nested_messages_link_each_level_once(ingestion_setup: Path) -> None:
    inner = BytesParser(policy=policy.default).parsebytes((FIXTURES / "upload.eml").read_bytes())
    outer = EmailMessage()
    outer["Subject"] = "Outer original message"
    outer.set_content("Read the nested email.")
    outer.add_attachment(inner, filename="nested.eml")
    response = TestClient(app).post("/v1/ingest", files={"file": ("outer.eml", outer.as_bytes())})
    assert response.status_code == 202, response.text
    result = response.json()["parse_result"]
    assert len(result["attachments"]) == 1
    nested = result["attachments"][0]
    assert nested["document"]["parent_document_id"] == result["document"]["id"]
    assert len(nested["attachments"]) == 1
    assert nested["attachments"][0]["document"]["parent_document_id"] == nested["document"]["id"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="System Tesseract not installed")
def test_real_image_ingestion(
    ingestion_setup: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {
        "languages": ["en"],
        "ocr_engine": "tesseract",
        "document_types": [],
        "departments": [],
        "routing_rules": [],
    }
    (tmp_path / "example.yaml").write_text(json.dumps(config))
    monkeypatch.setenv("ORG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("app.ingestion.parser_from_config", parser_from_config)
    response = TestClient(app).post(
        "/v1/ingest",
        files={
            "file": ("upload.png", (FIXTURES / "upload.png").read_bytes()),
        },
    )
    assert response.status_code == 202, response.text
    blocks = response.json()["parse_result"]["document"]["pages"][0]["blocks"]
    assert "fixture" in " ".join(item["text"] for item in blocks).lower()
