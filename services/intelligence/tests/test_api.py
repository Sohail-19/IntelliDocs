import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, RefResolver

from intellidocs_intelligence.main import app

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "contracts" / "schemas"
client = TestClient(app)


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
    response = client.post("/v1/ingest", json={"file_reference": "storage://example.pdf"})

    assert response.status_code == 202
    validate_contract(response.json(), "ingest-response.v1.schema.json")


def test_search_response_matches_contract() -> None:
    response = client.post("/v1/search", json={"query": "deadline", "top_k": 3})

    assert response.status_code == 200
    validate_contract(response.json(), "search-response.v1.schema.json")


def test_extract_obligations_response_matches_contract() -> None:
    response = client.post("/v1/extract-obligations", json={"doc_id": "doc-001"})

    assert response.status_code == 200
    validate_contract(response.json(), "extract-obligations-response.v1.schema.json")
