"""Configuration and reproducibility metadata for the combined evaluation."""

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "eval/gold/config.json"
Ablation = Literal["no_reranker", "dense_only", "sparse_only", "verifier_off", "plain_rag"]
Cutoff = Annotated[int, Field(ge=1, le=100)]
Threshold = Annotated[float, Field(gt=0, le=1)]
Name = Annotated[str, Field(pattern=r"\S")]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Embedding(Settings):
    backend: Literal["fake", "sentence_transformers", "bge_m3"]
    model: Name
    device: Name = "cpu"
    sparse: Literal["auto", "native", "bm25"] = "auto"
    document_prompt: str = ""
    query_prompt: str = ""
    batch_size: Annotated[int, Field(ge=1)] = 32
    bm25_k1: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.2
    bm25_b: Annotated[float, Field(ge=0, le=1)] = 0.75
    bm25_average_length: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 400


class Reranking(Settings):
    backend: Literal["fake", "cross_encoder"]
    model: Name
    device: Name = "cpu"
    batch_size: Annotated[int, Field(ge=1)] = 8
    max_length: Annotated[int, Field(ge=2)] = 512


class LLM(Settings):
    provider: Literal["fake", "gemini", "openai_compatible"]
    model: Name
    generation_retries: Annotated[int, Field(ge=0)] = 2


class Retrieval(Settings):
    recall_at: list[Cutoff] = Field(min_length=1)
    ndcg_at: Cutoff
    answer_top_k: Cutoff
    candidate_pool_size: Annotated[int, Field(ge=1)]
    rrf_k: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_cutoffs(self) -> Self:
        if len(set(self.recall_at)) != len(self.recall_at):
            raise ValueError("recall_at cutoffs must be unique")
        if self.candidate_pool_size < max(*self.recall_at, self.ndcg_at, self.answer_top_k):
            raise ValueError("candidate_pool_size must cover all retrieval and answer cutoffs")
        return self


class Thresholds(Settings):
    answer_quote_similarity: Threshold
    extraction_action_similarity: Threshold
    answer_support_check: bool = False


class OCRInputs(Settings):
    documents: Name
    ground_truth: Name
    manifest: Name
    org_config: Name


class RetrievalInputs(Settings):
    gold: Name
    chunks: Name


class AnswerInputs(RetrievalInputs):
    responses: Name | None = None
    sources: Name | None = None


class Predictions(Settings):
    gold: Name
    predictions: Name | None = None
    sources: Name | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if bool(self.predictions) != bool(self.sources):
            raise ValueError("Supply both predictions and sources for snapshot evaluation")
        return self


class Inputs(Settings):
    ocr: OCRInputs
    retrieval: RetrievalInputs
    answer: AnswerInputs
    extraction: Predictions
    relations: Predictions
    summary: Predictions


class Index(Settings):
    storage_root: Name
    url: Name
    collection: Name

    @model_validator(mode="after")
    def validate_public_url(self) -> Self:
        parsed = urlsplit(self.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Index URL must omit credentials, query and fragment")
        return self


class SuiteConfig(Settings):
    seed: int = Field(ge=0, lt=2**32)
    embedding: Embedding
    reranker: Reranking
    llm: LLM
    retrieval: Retrieval
    thresholds: Thresholds
    inputs: Inputs
    ablations: list[Ablation]
    live_index: Index | None = None

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        if len(set(self.ablations)) != len(self.ablations):
            raise ValueError("ablations must be unique")
        if self.inputs.answer.responses and (
            self.llm.provider != "fake" or self.thresholds.answer_support_check
        ):
            raise ValueError(
                "Answer fixture replay requires the fake provider and support check off"
            )
        stages = (self.inputs.extraction, self.inputs.relations, self.inputs.summary)
        if any(stage.predictions is None for stage in stages) and self.live_index is None:
            raise ValueError("Stages without prediction snapshots require live_index")
        return self


def path(name: str) -> Path:
    return ROOT / name


def fingerprint(source: Path) -> dict[str, str]:
    return {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


@contextmanager
def runtime_environment(config: SuiteConfig) -> Iterator[None]:
    embedding, reranker, llm = config.embedding, config.reranker, config.llm
    settings = {
        "EMBEDDING_BACKEND": embedding.backend,
        "EMBEDDING_MODEL": embedding.model,
        "EMBEDDING_DEVICE": embedding.device,
        "SPARSE_EMBEDDING_BACKEND": embedding.sparse,
        "EMBEDDING_DOCUMENT_PROMPT": embedding.document_prompt,
        "EMBEDDING_QUERY_PROMPT": embedding.query_prompt,
        "EMBEDDING_BATCH_SIZE": str(embedding.batch_size),
        "BM25_K1": str(embedding.bm25_k1),
        "BM25_B": str(embedding.bm25_b),
        "BM25_AVG_DOC_LENGTH": str(embedding.bm25_average_length),
        "RETRIEVAL_MODE": "hybrid",
        "RETRIEVAL_CANDIDATE_POOL_SIZE": str(config.retrieval.candidate_pool_size),
        "RETRIEVAL_RRF_K": str(config.retrieval.rrf_k),
        "RETRIEVAL_DEFAULT_TOP_K": str(config.retrieval.answer_top_k),
        "LLM_PROVIDER": llm.provider,
        "LLM_MODEL": llm.model,
        "GENERATION_MAX_RETRIES": str(llm.generation_retries),
        "VERIFICATION_FUZZY_THRESHOLD": str(config.thresholds.answer_quote_similarity),
        "VERIFICATION_SUPPORT_CHECK": str(config.thresholds.answer_support_check).lower(),
        "RERANKER_BACKEND": reranker.backend,
        "RERANKER_MODEL": reranker.model,
        "RERANKER_DEVICE": reranker.device,
        "RERANKER_BATCH_SIZE": str(reranker.batch_size),
        "RERANKER_MAX_LENGTH": str(reranker.max_length),
    }
    if config.live_index:
        settings.update(
            STORAGE_DIR=str(path(config.live_index.storage_root)),
            QDRANT_URL=config.live_index.url,
            QDRANT_COLLECTION=config.live_index.collection,
        )
    original = {name: os.environ.get(name) for name in settings}
    os.environ.update(settings)
    try:
        yield
    finally:
        for name, old in original.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
