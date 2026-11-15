import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, RefResolver

from intellidocs_intelligence.main import app

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "contracts" / "schemas"
client = TestClient(app)
pytestmark = pytest.mark.usefixtures("ingestion_setup")


def validate_contract(payload: object, schema_name: str) -> None:
    path = SCHEMAS / schema_name
    schema = json.loads(path.read_text(encoding="utf-8"))
    resolver = RefResolver(base_uri=f"{SCHEMAS.as_uri()}/", referrer=schema)
    Draft202012Validator(schema, resolver=resolver).validate(payload)


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


def test_search_response_matches_contract() -> None:
    response = client.post("/v1/search", json={"query": "deadline", "top_k": 3})

    assert response.status_code == 200
    validate_contract(response.json(), "search-response.v1.schema.json")


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


def test_extract_obligations_response_matches_contract() -> None:
    response = client.post("/v1/extract-obligations", json={"doc_id": "doc-001"})

    assert response.status_code == 200
    validate_contract(response.json(), "extract-obligations-response.v1.schema.json")
