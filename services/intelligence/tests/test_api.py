import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from intellidocs_intelligence.main import app

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "contracts" / "schemas"
client = TestClient(app)
pytestmark = pytest.mark.usefixtures("ingestion_setup")


def validate_contract(payload: object, schema_name: str) -> None:
    path = SCHEMAS / schema_name
    schema = json.loads(path.read_text(encoding="utf-8"))
    resolver = RefResolver(base_uri=f"{SCHEMAS.as_uri()}/", referrer=schema)
    Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker()).validate(
        payload
    )


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ingest_response_matches_contract() -> None:
    response = client.post(
        "/v1/ingest",
        files={
            "file": (
                "example.pdf",
                (Path(__file__).parent / "fixtures/digital.pdf").read_bytes(),
                "application/pdf",
            )
        },
        data={"org_id": "example"},
    )

    assert response.status_code == 202
    validate_contract(response.json(), "ingest-response.v1.schema.json")
    result = response.json()["parse_result"]
    assert result["document"]["id"] == response.json()["document_id"]
    assert result["document"]["filename"] == "example.pdf"
    assert result["document"]["pages"]
    assert result["document"]["status"] == "ready"


def test_index_requires_document_id() -> None:
    assert client.post("/v1/index", json={"doc_id": ""}).status_code == 422


def test_original_pdf_requires_the_stored_organization() -> None:
    data = (Path(__file__).parent / "fixtures/digital.pdf").read_bytes()
    upload = client.post(
        "/v1/ingest", files={"file": ("example.pdf", data)}, data={"org_id": "example"}
    )
    assert upload.status_code == 202
    route = f"/v1/documents/{upload.json()['document_id']}/source"
    downloaded = client.get(route, params={"org_id": "example"})
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.content == data
    assert client.get(route, params={"org_id": "another"}).status_code == 404


def test_ingest_without_org_id() -> None:
    response = client.post(
        "/v1/ingest",
        files={
            "file": (
                "example.pdf",
                (Path(__file__).parent / "fixtures/digital.pdf").read_bytes(),
            )
        },
    )
    assert response.status_code == 202
    validate_contract(response.json(), "ingest-response.v1.schema.json")


def test_ingest_requires_upload() -> None:
    assert client.post("/v1/ingest", data={"org_id": "example"}).status_code == 422


def test_extract_obligations_requires_document_id() -> None:
    response = client.post("/v1/extract-obligations", json={"doc_id": ""})

    assert response.status_code == 422
