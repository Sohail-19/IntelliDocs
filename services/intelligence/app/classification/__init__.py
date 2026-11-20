"""Optional type suggestions scoped to an organization's configured taxonomy."""

from pathlib import Path

from intellidocs_intelligence.models import OrgConfig

from .base import DocumentClassifier, TypePrediction, classification_text
from .tfidf import TfidfClassifier, TfidfModel
from .zero_shot import ZeroShotClassifier

__all__ = ["DocumentClassifier", "TypePrediction", "classification_text", "classifier_from_config"]


class _ThresholdClassifier(DocumentClassifier):
    def __init__(self, backends: list[DocumentClassifier], minimum: float) -> None:
        self.backends = backends
        self.minimum = minimum

    def predict(self, text: str) -> TypePrediction | None:
        for backend in self.backends:
            prediction = backend.predict(text)
            if prediction is not None and prediction.confidence >= self.minimum:
                return prediction
        return None


def classifier_from_config(org: OrgConfig) -> DocumentClassifier | None:
    settings = org.classification
    if settings is None or settings.backend == "disabled" or not org.document_types:
        return None
    backends: list[DocumentClassifier] = []
    if settings.backend == "tfidf" and settings.model_path is not None:
        try:
            artifact = TfidfModel.model_validate_json(Path(settings.model_path).read_bytes())
            backends.append(TfidfClassifier(artifact, org.document_types))
        except (OSError, ValueError):
            if not settings.llm_fallback:
                raise
    if settings.backend == "llm" or settings.llm_fallback:
        backends.append(ZeroShotClassifier(org.document_types))
    return _ThresholdClassifier(backends, settings.min_confidence)
