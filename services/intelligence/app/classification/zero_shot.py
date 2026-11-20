"""Ask the configured LLM to choose from a constrained document taxonomy."""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from string import Template

from intellidocs_intelligence.llm import LLMProvider, provider_from_env
from intellidocs_intelligence.models import ConfidenceContribution, ContractModel, NonBlankString

from .base import TEXT_LIMIT, DocumentClassifier, TypePrediction

PROMPT = Path(__file__).with_name("prompts") / "document_type.txt"


class ZeroShotResponse(ContractModel):
    document_type: NonBlankString
    confidence: ConfidenceContribution


class ZeroShotClassifier(DocumentClassifier):
    def __init__(
        self, allowed_types: Sequence[str], provider: Callable[[], LLMProvider] | None = None
    ) -> None:
        self.allowed_types = tuple(dict.fromkeys(allowed_types))
        self.provider = provider if provider is not None else provider_from_env

    def predict(self, text: str) -> TypePrediction | None:
        if not self.allowed_types or not text.strip():
            return None
        request = {"allowed_types": self.allowed_types, "excerpt": text[:TEXT_LIMIT]}
        prompt = Template(PROMPT.read_text(encoding="utf-8")).substitute(
            input_json=json.dumps(request, ensure_ascii=False)
        )
        schema = ZeroShotResponse.model_json_schema()
        schema["properties"]["document_type"] = {"type": "string", "enum": list(self.allowed_types)}
        try:
            response = self.provider().generate(prompt, schema)
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("Invalid response envelope from the classifier provider") from error
        draft = ZeroShotResponse.model_validate(response)
        if not draft.confidence or draft.document_type not in self.allowed_types:
            return None
        return TypePrediction(document_type=draft.document_type, confidence=draft.confidence)
