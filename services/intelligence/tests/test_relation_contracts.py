"""Check Phase 8 relation payloads against the shared v1 schemas."""

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError
from test_api import validate_contract

from intellidocs_intelligence.models import (
    ContractModel,
    DetectRelationsRequest,
    DocumentRelation,
    RelationResult,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


def example(name: str) -> dict[str, object]:
    return json.loads((EXAMPLES / f"{name}.v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name, model",
    [
        ("detect-relations-request", DetectRelationsRequest),
        ("document-relation", DocumentRelation),
        ("relation-result", RelationResult),
    ],
)
def test_relation_examples_round_trip(name: str, model: type[ContractModel]) -> None:
    value = example(name)
    validate_contract(value, f"{name}.v1.schema.json")
    assert model.model_validate(value).model_dump(mode="json", exclude_none=True) == value


@pytest.mark.parametrize(
    "field, value",
    [
        ("relation_type", "duplicates"),
        ("scope", "page"),
        ("status", "accepted"),
        ("confidence", 1.01),
        ("confidence", True),
        ("created_at", "2026-11-20T09:00:00"),
        ("created_at", "2026-11-20T09:00Z"),
        ("created_at", 1795165200),
        ("target_citations", [{"doc_id": "doc-001", "page": 2}]),
    ],
)
def test_relation_constraints_match_schema(field: str, value: object) -> None:
    invalid = {**example("document-relation"), field: value}
    with pytest.raises(ValidationError):
        DocumentRelation.model_validate(invalid)
    with pytest.raises(SchemaValidationError):
        validate_contract(invalid, "document-relation.v1.schema.json")


def test_result_can_report_no_relations() -> None:
    value = {"doc_id": "doc-002", "relations": [], "warnings": ["No earlier document found."]}
    validate_contract(value, "relation-result.v1.schema.json")
    assert RelationResult.model_validate(value).model_dump(mode="json") == value


@pytest.mark.parametrize(
    "changes", [{"source_citations": []}, {"target_citations": []}, {"needs_review": True}]
)
def test_confirmation_requires_two_sides_and_no_pending_review(changes: dict[str, object]) -> None:
    invalid = {**example("document-relation"), "status": "confirmed", **changes}
    with pytest.raises(ValidationError):
        DocumentRelation.model_validate(invalid)
    with pytest.raises(SchemaValidationError):
        validate_contract(invalid, "document-relation.v1.schema.json")
