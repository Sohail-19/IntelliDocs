"""Legacy and enriched v1 chunk compatibility, plus indexing contract checks."""

import json
import runpy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from intellidocs_intelligence.models import Chunk, IndexResult

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "contracts/schemas"
LEGACY = {
    "id": "chunk-001",
    "doc_id": "doc-001",
    "page": 1,
    "char_start": 0,
    "char_end": 4,
    "text": "Text",
}


def test_legacy_chunk_remains_valid() -> None:
    schema = json.loads((SCHEMAS / "chunk.v1.schema.json").read_text())
    Draft202012Validator(schema).validate(LEGACY)
    assert Chunk.model_validate(LEGACY).model_dump(mode="json", exclude_none=True) == LEGACY


def test_enriched_chunk_round_trip() -> None:
    example = json.loads((ROOT / "contracts/examples/chunk.v1.json").read_text())
    schema = json.loads((SCHEMAS / "chunk.v1.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(example)
    assert Chunk.model_validate(example).model_dump(mode="json", exclude_none=True) == example


@pytest.mark.parametrize(
    "field,value",
    [
        ("chunk_id", ""),
        ("page_start", 0),
        ("page_end", 0),
        ("block_ids", [""]),
        ("language", "x"),
        ("doc_date", "2026-02-30"),
    ],
)
def test_new_chunk_constraints(field: str, value: object) -> None:
    data = {**LEGACY, field: value}
    with pytest.raises(ValidationError):
        Chunk.model_validate(data)
    schema = json.loads((SCHEMAS / "chunk.v1.schema.json").read_text())
    assert not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(data)


def test_index_result_and_schema_definitions() -> None:
    for path in SCHEMAS.glob("*.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text()))
    example = json.loads((ROOT / "contracts/examples/index-result.v1.json").read_text())
    assert IndexResult.model_validate(example).model_dump() == example
    with pytest.raises(ValidationError):
        IndexResult.model_validate({**example, "chunk_count": -1})


def test_dependency_free_validator_rejects_invalid_document_date() -> None:
    validator = runpy.run_path(str(ROOT / "scripts/validate_contracts.py"))
    schema_path = SCHEMAS / "chunk.v1.schema.json"
    schemas = {path.resolve(): json.loads(path.read_text()) for path in SCHEMAS.glob("*.json")}
    with pytest.raises(validator["ContractError"], match="ISO date"):
        validator["validate_instance"](
            {**LEGACY, "doc_date": "not-a-date"},
            schemas[schema_path.resolve()],
            schema_path,
            schema_path,
            schemas,
        )
