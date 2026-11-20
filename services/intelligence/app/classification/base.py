"""Shared text input and prediction type for document classifiers."""

from abc import ABC, abstractmethod

from app.chunking.canonical import canonical_document
from intellidocs_intelligence.models import (
    ConfidenceContribution,
    ContractModel,
    NonBlankString,
    ParsedDocument,
)

TEXT_LIMIT = 16_000


class TypePrediction(ContractModel):
    document_type: NonBlankString
    confidence: ConfidenceContribution


class DocumentClassifier(ABC):
    @abstractmethod
    def predict(self, text: str) -> TypePrediction | None:
        """Suggest a configured type, or abstain if the evidence is insufficient."""


def classification_text(document: ParsedDocument) -> str:
    canonical = canonical_document(document)
    parts = []
    remaining = TEXT_LIMIT
    for span in canonical.blocks:
        if span.block.type == "header_footer":
            continue
        part = canonical.text[span.start : span.end].strip()[:remaining]
        if part:
            parts.append(part)
            remaining -= len(part) + 1
        if remaining <= 0:
            break
    return "\n".join(parts)
