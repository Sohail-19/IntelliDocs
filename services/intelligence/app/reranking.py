"""Rerank retrieved chunks with a lazily loaded sequence classifier."""

import math
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import import_module
from threading import Lock
from typing import Any

from intellidocs_intelligence.sparse import tokenize

from .retrieval import Candidate

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"


@dataclass(frozen=True)
class RerankerConfig:
    backend: str = "none"
    model_name: str = DEFAULT_MODEL
    device: str = "cpu"
    batch_size: int = 8
    max_length: int = 512

    def __post_init__(self) -> None:
        if self.backend not in {"none", "fake", "cross_encoder"}:
            raise ValueError("RERANKER_BACKEND must be none, fake, or cross_encoder")
        if not self.model_name.strip() or not self.device.strip():
            raise ValueError("RERANKER_MODEL and RERANKER_DEVICE must not be empty")
        if type(self.batch_size) is not int or self.batch_size < 1:
            raise ValueError("RERANKER_BATCH_SIZE must be a positive integer")
        if type(self.max_length) is not int or self.max_length < 2:
            raise ValueError("RERANKER_MAX_LENGTH must be an integer of at least 2")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RerankerConfig":
        values = os.environ if env is None else env
        try:
            return cls(
                backend=values.get("RERANKER_BACKEND", "none"),
                model_name=values.get("RERANKER_MODEL", DEFAULT_MODEL),
                device=values.get("RERANKER_DEVICE", "cpu"),
                batch_size=int(values.get("RERANKER_BATCH_SIZE", "8")),
                max_length=int(values.get("RERANKER_MAX_LENGTH", "512")),
            )
        except ValueError as error:
            raise ValueError(f"Invalid reranker configuration: {error}") from error


class Reranker(ABC):
    @abstractmethod
    def rerank(self, query: str, candidates: Sequence[Candidate], top_n: int) -> list[Candidate]:
        """Return up to top_n candidates in reranker order."""


def _validate_limit(top_n: int) -> None:
    if type(top_n) is not int or top_n < 0:
        raise ValueError("top_n must be a nonnegative integer")


def _rank(candidates: Sequence[Candidate], scores: Sequence[float], top_n: int) -> list[Candidate]:
    pairs = sorted(zip(candidates, scores, strict=True), key=lambda pair: -pair[1])
    return [replace(candidate, rerank_score=score) for candidate, score in pairs[:top_n]]


class NoOpReranker(Reranker):
    def rerank(self, query: str, candidates: Sequence[Candidate], top_n: int) -> list[Candidate]:
        _validate_limit(top_n)
        return list(candidates[:top_n])


class FakeReranker(Reranker):
    """Score by the number of distinct shared terms."""

    def rerank(self, query: str, candidates: Sequence[Candidate], top_n: int) -> list[Candidate]:
        _validate_limit(top_n)
        if top_n == 0:
            return []
        terms = set(tokenize(query))
        scores = [float(len(terms.intersection(tokenize(item.chunk.text)))) for item in candidates]
        return _rank(candidates, scores, top_n)


class CrossEncoderReranker(Reranker):
    """Score each query and chunk jointly with a one-logit Transformers model."""

    def __init__(self, config: RerankerConfig | None = None) -> None:
        self.config = config if config is not None else RerankerConfig.from_env()
        self._runtime: tuple[Any, Any, Any, int] | None = None
        self._lock = Lock()

    def _load(self) -> tuple[Any, Any, Any, int]:
        with self._lock:
            if self._runtime is None:
                self._runtime = self._load_runtime()
            return self._runtime

    def _load_runtime(self) -> tuple[Any, Any, Any, int]:
        try:
            torch = import_module("torch")
            transformers = import_module("transformers")
        except ImportError as error:
            raise RuntimeError("Install the intelligence [reranker] extra") from error

        # Both loaders resolve local directories or previously provisioned Hub weights.
        options = {"local_files_only": True, "trust_remote_code": False}
        tokenizer = transformers.AutoTokenizer.from_pretrained(self.config.model_name, **options)
        max_length = min(self.config.max_length, tokenizer.model_max_length)
        if max_length < tokenizer.num_special_tokens_to_add(pair=True) + 2:
            raise ValueError("RERANKER_MAX_LENGTH must fit both texts and the pair special tokens")
        model = transformers.AutoModelForSequenceClassification.from_pretrained(
            self.config.model_name, **options
        )
        if model.config.num_labels != 1:
            raise ValueError("Reranker model must produce one relevance logit per pair")
        model.to(self.config.device)
        model.eval()
        return tokenizer, model, torch.inference_mode, max_length

    def rerank(self, query: str, candidates: Sequence[Candidate], top_n: int) -> list[Candidate]:
        _validate_limit(top_n)
        if not candidates or top_n == 0:
            return []
        tokenizer, model, inference_mode, max_length = self._load()
        scores: list[float] = []
        for start in range(0, len(candidates), self.config.batch_size):
            batch = candidates[start : start + self.config.batch_size]
            # Fast tokenizers set padding and truncation state during the call.
            with self._lock:
                inputs = tokenizer(
                    [query] * len(batch),
                    [item.chunk.text for item in batch],
                    padding=True,
                    truncation="longest_first",
                    max_length=max_length,
                    return_tensors="pt",
                )
            with inference_mode():
                logits = model(**inputs.to(self.config.device), return_dict=True).logits
                batch_scores = logits.reshape(-1).float().tolist()
            if len(batch_scores) != len(batch) or not all(map(math.isfinite, batch_scores)):
                raise ValueError("Expected one finite rerank score per candidate")
            scores.extend(batch_scores)
        return _rank(candidates, scores, top_n)


def create_reranker(config: RerankerConfig | None = None) -> Reranker:
    settings = config if config is not None else RerankerConfig.from_env()
    if settings.backend == "cross_encoder":
        return CrossEncoderReranker(settings)
    if settings.backend == "fake":
        return FakeReranker()
    return NoOpReranker()
