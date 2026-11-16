"""Configuration for internal retrieval and evaluation."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


def validate_top_k(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("top_k must be an integer between 1 and 100")
    return value


@dataclass(frozen=True)
class RetrievalConfig:
    mode: str = "hybrid"
    candidate_pool_size: int = 50
    rrf_k: int = 2
    default_top_k: int = 10

    def __post_init__(self) -> None:
        if self.mode not in {"hybrid", "dense", "sparse"}:
            raise ValueError("RETRIEVAL_MODE must be hybrid, dense, or sparse")
        if type(self.candidate_pool_size) is not int or self.candidate_pool_size < 1:
            raise ValueError("RETRIEVAL_CANDIDATE_POOL_SIZE must be a positive integer")
        if type(self.rrf_k) is not int or self.rrf_k < 1:
            raise ValueError("RETRIEVAL_RRF_K must be a positive integer")
        validate_top_k(self.default_top_k)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RetrievalConfig":
        values = os.environ if env is None else env
        try:
            return cls(
                mode=values.get("RETRIEVAL_MODE", "hybrid"),
                candidate_pool_size=int(values.get("RETRIEVAL_CANDIDATE_POOL_SIZE", "50")),
                rrf_k=int(values.get("RETRIEVAL_RRF_K", "2")),
                default_top_k=int(values.get("RETRIEVAL_DEFAULT_TOP_K", "10")),
            )
        except ValueError as error:
            raise ValueError(f"Invalid retrieval configuration: {error}") from error
