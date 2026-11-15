"""Verify richer document payloads and their contract boundaries."""

import json
import runpy
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver
from pydantic import ValidationError

from intellidocs_intelligence.models import Document, ParseResult

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "contracts/schemas"


def test_document_example_round_trip_and_legacy_compatibility() -> None:
    example = json.loads((ROOT / "contracts/examples/document.v1.json").read_text())
    parsed = Document.model_validate(example)
    assert parsed.model_dump(mode="json", exclude_none=True) == example
    del example["pages"]
    Document.model_validate(example)
    Draft202012Validator(json.loads((SCHEMAS / "document.v1.schema.json").read_text())).validate(
        example
    )


@pytest.mark.parametrize("problem", ["bbox_short", "bbox_long", "missing_rows", "missing_pages"])
def test_invalid_parsed_payloads_rejected_by_models_and_validators(problem: str) -> None:
    document = json.loads((ROOT / "contracts/examples/document.v1.json").read_text())
    result = json.loads((ROOT / "contracts/examples/parse-result.v1.json").read_text())
    result["document"] = deepcopy(document)
    block = result["document"]["pages"][0]["blocks"][2]
    if problem == "bbox_short":
        block["bbox"] = [0, 0, 10]
    elif problem == "bbox_long":
        block["bbox"] = [0, 0, 10, 10, 20]
    elif problem == "missing_rows":
        del block["rows"]
    else:
        del result["document"]["pages"]

    with pytest.raises(ValidationError):
        ParseResult.model_validate(result)

    schema_path = SCHEMAS / "parse-result.v1.schema.json"
    schema = json.loads(schema_path.read_text())
    resolver = RefResolver(base_uri=f"{SCHEMAS.as_uri()}/", referrer=schema)
    assert not Draft202012Validator(schema, resolver=resolver).is_valid(result)

    # Exercise the dependency-free CI validator against the same invalid payloads.
    validator = runpy.run_path(str(ROOT / "scripts/validate_contracts.py"))
    schemas = {path.resolve(): json.loads(path.read_text()) for path in SCHEMAS.glob("*.json")}
    with pytest.raises(validator["ContractError"]):
        validator["validate_instance"](result, schema, schema_path, schema_path, schemas)
