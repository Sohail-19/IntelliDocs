"""Rich extraction contracts, legacy compatibility, and schema/model parity."""

import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError
from test_api import validate_contract

from intellidocs_intelligence.models import (
    ExtractionResult,
    ExtractionStats,
    ExtractObligationsResponse,
    Obligation,
    ObligationDeadline,
    ObligationOwner,
)

ROOT = Path(__file__).resolve().parents[3]


def example(name: str) -> dict:
    return json.loads((ROOT / f"contracts/examples/{name}.v1.json").read_text())


@pytest.mark.parametrize(
    "name,model",
    [
        ("obligation-owner", ObligationOwner),
        ("obligation-deadline", ObligationDeadline),
        ("obligation", Obligation),
        ("extraction-stats", ExtractionStats),
        ("extraction-result", ExtractionResult),
        ("extract-obligations-response", ExtractObligationsResponse),
    ],
)
def test_examples_round_trip(name: str, model: type) -> None:
    value = example(name)
    validate_contract(value, f"{name}.v1.schema.json")
    assert model.model_validate(value).model_dump(mode="json", exclude_unset=True) == value


def test_obligation_default_serialization_keeps_required_nulls_and_omits_source() -> None:
    value = example("obligation")
    payload = Obligation.model_validate(value).model_dump(mode="json")
    assert payload == value
    validate_contract(payload, "obligation.v1.schema.json")
    assert payload["owner"]["resolved_department"] is None
    assert payload["deadline"]["normalized_date"] is None


@pytest.mark.parametrize("status", ["open", "in_progress", "complete", "cancelled"])
def test_legacy_obligations_remain_valid(status: str) -> None:
    legacy = example("extract-obligations-response")["obligations"][0]
    legacy["status"] = status
    validate_contract(legacy, "obligation.v1.schema.json")
    assert Obligation.model_validate(legacy).model_dump(mode="json") == legacy


@pytest.mark.parametrize("status", ["extracted", "verified", "needs_review", "rejected"])
def test_new_statuses_and_absolute_resolved_dates(status: str) -> None:
    value = example("obligation")
    value["status"] = status
    value["deadline"].update(
        {
            "raw_text": "5 December 2026",
            "normalized_date": "2026-12-05",
            "is_relative": False,
            "resolved": True,
        }
    )
    validate_contract(value, "obligation.v1.schema.json")
    assert Obligation.model_validate(value).model_dump(mode="json") == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("doc_id", None),
        ("citations", None),
        ("extractor_version", None),
        ("obligation_type", "routing"),
        ("confidence", 1.1),
        ("status", "open"),
        ("owner.resolved", "false"),
        ("owner.resolved_department", 42),
        ("deadline.normalized_date", "2026-02-30"),
        ("deadline.anchor_date", "20261205"),
        ("deadline.is_relative", 1),
        ("citations", [{"doc_id": "doc-001"}]),
    ],
)
def test_bad_structured_fields_fail_both_schema_and_model(field: str, value: object) -> None:
    payload = example("obligation")
    if "." in field:
        parent, child = field.split(".")
        payload[parent][child] = value
    elif value is None:
        del payload[field]
    else:
        payload[field] = value
    with pytest.raises(ValidationError):
        Obligation.model_validate(payload)
    with pytest.raises(SchemaValidationError):
        validate_contract(payload, "obligation.v1.schema.json")


def test_extraction_result_requires_metadata_and_integer_stats() -> None:
    value = example("extraction-result")
    validate_contract(
        ExtractionResult.model_validate(value).model_dump(mode="json"),
        "extraction-result.v1.schema.json",
    )
    for field in ("doc_id", "warnings", "stats"):
        invalid = {key: item for key, item in value.items() if key != field}
        with pytest.raises(ValidationError):
            ExtractionResult.model_validate(invalid)
        with pytest.raises(SchemaValidationError):
            validate_contract(invalid, "extraction-result.v1.schema.json")
    for total in (-1, True, 1.5):
        invalid = {**value, "stats": {**value["stats"], "total": total}}
        with pytest.raises(ValidationError):
            ExtractionResult.model_validate(invalid)
        with pytest.raises(SchemaValidationError):
            validate_contract(invalid, "extraction-result.v1.schema.json")


def test_standalone_validator_checks_union_branches_and_nullable_fields() -> None:
    spec = importlib.util.spec_from_file_location(
        "validate_contracts", ROOT / "scripts/validate_contracts.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = list(module.SCHEMAS.glob("*.v1.schema.json"))
    schemas = {path.resolve(): module.load_json(path) for path in paths}
    path = (module.SCHEMAS / "obligation.v1.schema.json").resolve()
    source = module.EXAMPLES / "obligation.v1.json"
    valid = example("obligation")
    module.validate_instance(valid, schemas[path], source, path, schemas)
    for bad in [
        {**valid, "owner": "legacy owner"},
        {**valid, "deadline": {**valid["deadline"], "anchor_date": "2026-02-30"}},
    ]:
        with pytest.raises(module.ContractError):
            module.validate_instance(bad, schemas[path], source, path, schemas)
