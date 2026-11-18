"""Check the answer contract and Python representations against shared examples."""

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError
from test_api import validate_contract

from intellidocs_intelligence.models import Answer, AnswerRequest, Claim, ContractModel

ROOT = Path(__file__).resolve().parents[3]


def example(name: str) -> dict:
    return json.loads((ROOT / f"contracts/examples/{name}.v1.json").read_text())


@pytest.mark.parametrize(
    "name,model", [("answer-request", AnswerRequest), ("claim", Claim), ("answer", Answer)]
)
def test_examples_round_trip(name: str, model: type[ContractModel]) -> None:
    value = example(name)
    validate_contract(value, f"{name}.v1.schema.json")
    assert model.model_validate(value).model_dump(mode="json", exclude_none=True) == value


def test_abstained_answer_can_have_empty_text_claims_and_retrieved_ids() -> None:
    value = {
        **example("answer"),
        "answer_text": "",
        "claims": [],
        "abstained": True,
        "abstain_reason": "No supporting source was retrieved.",
        "retrieved_chunk_ids": [],
    }
    validate_contract(value, "answer.v1.schema.json")
    assert Answer.model_validate(value).model_dump(mode="json") == value


@pytest.mark.parametrize(
    "field,value", [("query", "  "), ("top_k", 0), ("top_k", True), ("language", "e")]
)
def test_request_constraints_match_schema(field: str, value: object) -> None:
    request = {**example("answer-request"), field: value}
    with pytest.raises(ValidationError):
        AnswerRequest.model_validate(request)
    with pytest.raises(SchemaValidationError):
        validate_contract(request, "answer-request.v1.schema.json")


def test_claim_status_and_nested_citations_match_shared_contract() -> None:
    invalid = example("claim")
    invalid["status"] = "supported"
    with pytest.raises(ValidationError):
        Claim.model_validate(invalid)
    with pytest.raises(SchemaValidationError):
        validate_contract(invalid, "claim.v1.schema.json")

    invalid = example("answer")
    del invalid["claims"][0]["citations"][0]["doc_id"]
    with pytest.raises(ValidationError):
        Answer.model_validate(invalid)
    with pytest.raises(SchemaValidationError):
        validate_contract(invalid, "answer.v1.schema.json")
