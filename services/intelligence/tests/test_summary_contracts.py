"""Exercise the shared summary contracts without running generation."""

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError
from test_api import validate_contract

from intellidocs_intelligence.models import (
    ContractModel,
    OrgConfig,
    Summary,
    SummaryProfile,
    SummaryRequest,
)

ROOT = Path(__file__).resolve().parents[3]


def example(name: str) -> dict[str, object]:
    return json.loads((ROOT / "contracts" / "examples" / f"{name}.v1.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "name, model",
    [
        ("summary", Summary),
        ("summary-request", SummaryRequest),
        ("summary-profile", SummaryProfile),
    ],
)
def test_summary_examples_round_trip(name: str, model: type[ContractModel]) -> None:
    value = example(name)
    validate_contract(value, f"{name}.v1.schema.json")
    assert model.model_validate(value).model_dump(mode="json", exclude_unset=True) == value


@pytest.mark.parametrize(
    "audience", [{"department": "finance"}, {"department": None, "role": "finance_officer"}]
)
def test_either_selector_is_sufficient(audience: dict[str, object]) -> None:
    request = {"doc_id": "doc-001", "language": "hi", **audience}
    validate_contract(request, "summary-request.v1.schema.json")
    assert SummaryRequest.model_validate(request).language == "hi"

    response = {**example("summary"), "audience": audience}
    validate_contract(response, "summary.v1.schema.json")
    Summary.model_validate(response)


@pytest.mark.parametrize("audience", [{}, {"role": None}, {"department": " "}])
def test_request_must_name_an_audience(audience: dict[str, object]) -> None:
    value = {"doc_id": "doc-001", "language": "en", **audience}
    with pytest.raises(ValidationError):
        SummaryRequest.model_validate(value)
    with pytest.raises(SchemaValidationError):
        validate_contract(value, "summary-request.v1.schema.json")


@pytest.mark.parametrize(
    "changes",
    [
        {"points": [{"text": "Uncited point", "citations": []}]},
        {"not_relevant": True},
        {"not_relevant": True, "points": [], "relevance_reason": "  "},
        {"audience": {}},
    ],
)
def test_summary_requires_grounded_points_and_consistent_relevance(
    changes: dict[str, object],
) -> None:
    value = {**example("summary"), **changes}
    with pytest.raises(ValidationError):
        Summary.model_validate(value)
    with pytest.raises(SchemaValidationError):
        validate_contract(value, "summary.v1.schema.json")


def test_irrelevant_document_can_return_no_points_and_a_reason() -> None:
    value = {
        **example("summary"),
        "not_relevant": True,
        "points": [],
        "relevance_reason": "The document contains no reporting or payment requirements.",
    }
    validate_contract(value, "summary.v1.schema.json")
    assert Summary.model_validate(value).not_relevant


def test_org_examples_cover_all_departments_and_legacy_config_still_loads() -> None:
    config = json.loads((ROOT / "config" / "orgs" / "example.yaml").read_text("utf-8"))
    validate_contract(config, "org-config.v1.schema.json")
    assert config["summary"] == example("org-config")["summary"]
    org = OrgConfig.model_validate(config)
    assert org.summary is not None
    assert set(org.summary.department_profiles) == set(org.departments)
    assert all(
        set(profile.glossary) == set(org.languages)
        for profile in (
            *org.summary.department_profiles.values(),
            *org.summary.role_profiles.values(),
        )
    )

    del config["summary"]
    validate_contract(config, "org-config.v1.schema.json")
    assert OrgConfig.model_validate(config).summary is None
