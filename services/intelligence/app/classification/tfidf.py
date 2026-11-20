"""Sparse TF-IDF and a multinomial logistic model stored as plain JSON."""

import math
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from random import Random
from tempfile import NamedTemporaryFile
from typing import Literal, Self

from pydantic import Field, FiniteFloat, model_validator

from intellidocs_intelligence.models import ContractModel
from intellidocs_intelligence.sparse import tokenize

from .base import DocumentClassifier, TypePrediction


class TfidfModel(ContractModel):
    version: Literal["tfidf-logistic.v1"] = "tfidf-logistic.v1"
    idf: dict[str, FiniteFloat] = Field(min_length=1)
    weights: dict[str, dict[str, FiniteFloat]] = Field(min_length=2)
    bias: dict[str, FiniteFloat]

    @model_validator(mode="after")
    def check_dimensions(self) -> Self:
        if self.weights.keys() != self.bias.keys() or any(
            not terms.keys() <= self.idf.keys() for terms in self.weights.values()
        ):
            raise ValueError("Incompatible logistic model weights, vocabulary, or labels")
        if any(not token or value <= 0 for token, value in self.idf.items()):
            raise ValueError("Invalid TF-IDF vocabulary")
        return self

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(self.model_dump_json().encode("utf-8"))
                output.flush()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)


def vector(text: str, idf: Mapping[str, float]) -> dict[str, float]:
    counts = Counter(token for token in tokenize(text) if token in idf)
    weighted = {term: (1 + math.log(count)) * idf[term] for term, count in counts.items()}
    norm = math.sqrt(sum(value * value for value in weighted.values()))
    return {term: value / norm for term, value in weighted.items()} if norm else {}


def probabilities(
    features: Mapping[str, float],
    weights: Mapping[str, Mapping[str, float]],
    bias: Mapping[str, float],
) -> dict[str, float]:
    logits = {
        label: bias[label] + sum(row.get(term, 0.0) * value for term, value in features.items())
        for label, row in weights.items()
    }
    largest = max(logits.values())
    values = {label: math.exp(logit - largest) for label, logit in logits.items()}
    total = sum(values.values())
    return {label: value / total for label, value in values.items()}


class TfidfClassifier(DocumentClassifier):
    def __init__(self, model: TfidfModel, allowed_types: Collection[str]) -> None:
        if set(model.weights) != set(allowed_types):
            raise ValueError("Model labels must match the organization's document types")
        self.model = model

    def predict(self, text: str) -> TypePrediction | None:
        features = vector(text, self.model.idf)
        if not features:
            return None
        scores = probabilities(features, self.model.weights, self.model.bias)
        label = max(scores, key=scores.__getitem__)
        return TypePrediction(document_type=label, confidence=scores[label])


def train_tfidf(samples: Sequence[tuple[str, str]], allowed_types: Collection[str]) -> TfidfModel:
    """Fit the IDF on training text and optimize softmax cross-entropy by SGD."""
    labels = sorted(set(allowed_types))
    if len(labels) < 2:
        raise ValueError("Text training requires at least two configured document types")
    present = {label for _, label in samples}
    if present != set(labels):
        raise ValueError(
            f"Training labels differ from config. Unknown: {sorted(present - set(labels))}; "
            f"missing: {sorted(set(labels) - present)}"
        )
    frequencies = Counter(term for text, _ in samples for term in set(tokenize(text)))
    idf = {
        term: 1 + math.log((1 + len(samples)) / (1 + frequencies[term]))
        for term in sorted(frequencies)
    }
    rows = [(vector(text, idf), label) for text, label in samples]
    if any(not features for features, _ in rows):
        raise ValueError("Each training document needs nonempty parsed text")
    weights: dict[str, dict[str, float]] = {label: {} for label in labels}
    bias = dict.fromkeys(labels, 0.0)
    random = Random(42)
    for epoch in range(40):
        random.shuffle(rows)
        rate = 0.45 / math.sqrt(epoch + 1)
        for features, expected in rows:
            scores = probabilities(features, weights, bias)
            for label, row in weights.items():
                update = rate * (float(label == expected) - scores[label])
                bias[label] += update
                for term, value in features.items():
                    row[term] = row.get(term, 0.0) + update * value
    return TfidfModel(idf=idf, weights=weights, bias=bias)
