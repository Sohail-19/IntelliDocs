"""Dense/sparse embedding boundary with deterministic and lazy offline adapters."""

from __future__ import annotations

import hashlib
import math
import os
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Sequence
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from threading import Lock
from typing import Any

from typing_extensions import TypedDict

from .runtime import flag
from .sparse import Bm25Encoder

LIGHTWEIGHT_MODEL = "intfloat/multilingual-e5-small"


def embedding_settings_key() -> tuple[str | None, ...]:
    """All environment settings that select a cached adapter instance."""
    return tuple(
        os.environ.get(name)
        for name in (
            "EMBEDDING_LIGHTWEIGHT",
            "EMBEDDING_LIGHTWEIGHT_MODEL",
            "EMBEDDING_BACKEND",
            "EMBEDDING_MODEL",
            "EMBEDDING_DEVICE",
            "EMBEDDING_DOCUMENT_PROMPT",
            "EMBEDDING_QUERY_PROMPT",
            "SPARSE_EMBEDDING_BACKEND",
            "BM25_K1",
            "BM25_B",
            "BM25_AVG_DOC_LENGTH",
        )
    )


@lru_cache(maxsize=8)
def _cached_embedder(settings: tuple[str | None, ...]) -> Embedder:
    return embedder_from_env()


def configured_embedder() -> Embedder:
    """Share the configured model between indexing and query encoding."""
    return _cached_embedder(embedding_settings_key())


class SparseVector(TypedDict):
    indices: list[int]
    values: list[float]


class Embedding(TypedDict):
    dense: list[float]
    sparse: SparseVector


class Embedder(ABC):
    model_name: str
    supports_native_sparse = False
    sparse_requires_idf = False

    def load(self) -> None:
        """Initialize provisioned weights without encoding, for cold-start measurement."""
        return None

    def cache_key(self) -> dict[str, object]:
        return {
            "adapter": f"{type(self).__module__}.{type(self).__qualname__}",
            "model": self.model_name,
        }

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """Return one dense/sparse embedding per text, in input order."""

    def embed_queries(self, texts: Sequence[str]) -> list[Embedding]:
        """Models without distinct query prompts share their document representation."""
        return self.embed(texts)


def _texts(texts: Sequence[str]) -> list[str]:
    if isinstance(texts, str) or any(not isinstance(text, str) for text in texts):
        raise TypeError("texts must be a sequence of strings")
    return list(texts)


class FakeEmbedder(Embedder):
    """Stable hashing, not semantic embeddings; requires no third-party libraries."""

    model_name = "fake-sha256-v1"
    supports_native_sparse = True

    def __init__(self, dimensions: int = 16) -> None:
        if dimensions <= 0:
            raise ValueError("Fake dense dimensions must be positive")
        self.dimensions = dimensions

    def cache_key(self) -> dict[str, object]:
        return {**super().cache_key(), "dimensions": self.dimensions}

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        embeddings: list[Embedding] = []
        for text in _texts(texts):
            dense = [0.0] * self.dimensions
            sparse: Counter[int] = Counter()
            for token in text.split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big")
                sparse[index] += 1
                dense[index % self.dimensions] += 1.0 if digest[4] % 2 else -1.0
            length = math.sqrt(sum(value * value for value in dense))
            if length:
                dense = [value / length for value in dense]
            indices = sorted(sparse)
            embeddings.append(
                {
                    "dense": dense,
                    "sparse": {
                        "indices": indices,
                        "values": [float(sparse[index]) for index in indices],
                    },
                }
            )
        return embeddings


class BgeM3Embedder(Embedder):
    """Load provisioned BGE-M3 weights on the first nonempty embed call."""

    supports_native_sparse = True

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        self.model_name = (
            model_name
            if model_name is not None
            else os.environ.get(
                "EMBEDDING_MODEL",
                "BAAI/bge-m3",
            )
        )
        self.device = device if device is not None else os.environ.get("EMBEDDING_DEVICE", "cpu")
        if not self.model_name.strip() or not self.device.strip():
            raise ValueError("EMBEDDING_MODEL and EMBEDDING_DEVICE must not be empty")
        self._model: Any = None
        self._load_lock = Lock()

    def cache_key(self) -> dict[str, object]:
        return {**super().cache_key(), "device": self.device}

    def load(self) -> None:
        self._load()

    def _load(self) -> Any:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    os.environ["HF_HUB_OFFLINE"] = "1"
                    os.environ["TRANSFORMERS_OFFLINE"] = "1"
                    source = Path(self.model_name).expanduser()
                    if not source.is_dir():
                        try:
                            hub = import_module("huggingface_hub")
                            source = Path(
                                hub.snapshot_download(
                                    repo_id=self.model_name,
                                    local_files_only=True,
                                )
                            )
                        except ImportError as error:
                            raise RuntimeError("Install the intelligence [bge-m3] extra") from error
                        except (OSError, ValueError) as error:
                            raise RuntimeError(
                                "BGE-M3 weights are not available locally; "
                                "provision EMBEDDING_MODEL before runtime (downloads are disabled)"
                            ) from error
                    try:
                        module = import_module("FlagEmbedding")
                    except ImportError as error:
                        raise RuntimeError("Install the intelligence [bge-m3] extra") from error
                    self._model = module.BGEM3FlagModel(
                        str(source),
                        devices=self.device,
                        use_fp16=False,
                        normalize_embeddings=True,
                        trust_remote_code=False,
                    )
        return self._model

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        inputs = _texts(texts)
        if not inputs:
            return []
        output = self._load().encode(
            inputs,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense_rows, sparse_rows = output["dense_vecs"], output["lexical_weights"]
        if len(dense_rows) != len(inputs) or len(sparse_rows) != len(inputs):
            raise ValueError("Embedding model returned the wrong batch size")
        embeddings: list[Embedding] = []
        dimensions: int | None = None
        for dense_row, weights in zip(dense_rows, sparse_rows, strict=True):
            dense = [float(value) for value in dense_row]
            pairs = sorted((int(index), float(value)) for index, value in weights.items())
            if not dense or not all(math.isfinite(value) for value in dense):
                raise ValueError("Dense embeddings must be nonempty and finite")
            if dimensions is not None and len(dense) != dimensions:
                raise ValueError("Dense embedding dimensions differ within the batch")
            dimensions = len(dense)
            if any(index < 0 or not math.isfinite(value) or value < 0 for index, value in pairs):
                raise ValueError("Sparse embeddings require nonnegative indices and finite weights")
            if len({index for index, _value in pairs}) != len(pairs):
                raise ValueError("Sparse embedding indices must be unique")
            embeddings.append(
                {
                    "dense": dense,
                    "sparse": {
                        "indices": [index for index, _value in pairs],
                        "values": [value for _index, value in pairs],
                    },
                }
            )
        return embeddings


class SentenceTransformerEmbedder(Embedder):
    """Offline dense adapter; the factory supplies the configured sparse representation."""

    def __init__(
        self, model_name: str, device: str, prompt: str = "", query_prompt: str = ""
    ) -> None:
        if not model_name.strip() or not device.strip():
            raise ValueError("EMBEDDING_MODEL and EMBEDDING_DEVICE must not be empty")
        self.model_name, self.device, self.prompt = model_name, device, prompt
        self.query_prompt = query_prompt
        self._model: Any = None
        self._load_lock = Lock()

    def cache_key(self) -> dict[str, object]:
        return {**super().cache_key(), "device": self.device, "prompt": self.prompt}

    def load(self) -> None:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    os.environ["HF_HUB_OFFLINE"] = "1"
                    os.environ["TRANSFORMERS_OFFLINE"] = "1"
                    try:
                        module = import_module("sentence_transformers")
                    except ImportError as error:
                        raise RuntimeError(
                            "Install the intelligence [sentence-transformers] extra"
                        ) from error
                    try:
                        self._model = module.SentenceTransformer(
                            self.model_name,
                            device=self.device,
                            local_files_only=True,
                            trust_remote_code=False,
                        )
                    except (OSError, ValueError) as error:
                        raise RuntimeError(
                            "Embedding weights are not available locally; "
                            "provision the configured model before runtime (downloads are disabled)"
                        ) from error

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        return self._encode(texts, self.prompt)

    def embed_queries(self, texts: Sequence[str]) -> list[Embedding]:
        return self._encode(texts, self.query_prompt)

    def _encode(self, texts: Sequence[str], prompt: str) -> list[Embedding]:
        inputs = _texts(texts)
        if not inputs:
            return []
        self.load()
        # Pass an explicit prompt, including an empty one, to avoid model defaults
        # silently changing the indexed representation. E5 passages need 'passage: '.
        rows = self._model.encode(
            inputs,
            prompt=prompt,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        if len(rows) != len(inputs):
            raise ValueError("Embedding model returned the wrong batch size")
        vectors: list[Embedding] = []
        dimension: int | None = None
        for row in rows:
            dense = [float(value) for value in row]
            if not dense or not all(math.isfinite(value) for value in dense):
                raise ValueError("Dense embeddings must be nonempty and finite")
            if dimension is not None and len(dense) != dimension:
                raise ValueError("Dense embedding dimensions differ within the batch")
            dimension = len(dense)
            vectors.append({"dense": dense, "sparse": {"indices": [], "values": []}})
        return vectors


class SparseOverrideEmbedder(Embedder):
    """Combine a dense adapter with BM25, or explicitly disable its sparse side."""

    def __init__(self, delegate: Embedder, encoder: Bm25Encoder | None) -> None:
        self.delegate, self.encoder = delegate, encoder
        self.model_name = delegate.model_name
        self.sparse_requires_idf = encoder is not None

    def load(self) -> None:
        self.delegate.load()

    def cache_key(self) -> dict[str, object]:
        return {
            "adapter": f"{type(self).__module__}.{type(self).__qualname__}",
            "dense": self.delegate.cache_key(),
            "sparse": self.encoder.cache_key() if self.encoder is not None else "none",
        }

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        return self._encode(texts, query=False)

    def embed_queries(self, texts: Sequence[str]) -> list[Embedding]:
        return self._encode(texts, query=True)

    def _encode(self, texts: Sequence[str], *, query: bool) -> list[Embedding]:
        inputs = _texts(texts)
        vectors = self.delegate.embed_queries(inputs) if query else self.delegate.embed(inputs)
        if len(vectors) != len(inputs):
            raise ValueError("Embedder returned the wrong batch size")
        return [
            {
                "dense": vector["dense"],
                "sparse": (self.encoder.query(text) if query else self.encoder.document(text))
                if self.encoder is not None
                else {"indices": [], "values": []},
            }
            for text, vector in zip(inputs, vectors, strict=True)
        ]


def embedder_from_env() -> Embedder:
    backend = os.environ.get("EMBEDDING_BACKEND", "fake")
    embedder: Embedder
    if flag(os.environ, "EMBEDDING_LIGHTWEIGHT", False):
        embedder = SentenceTransformerEmbedder(
            os.environ.get("EMBEDDING_LIGHTWEIGHT_MODEL") or LIGHTWEIGHT_MODEL,
            os.environ.get("EMBEDDING_DEVICE", "cpu"),
            os.environ.get("EMBEDDING_DOCUMENT_PROMPT") or "passage: ",
            os.environ.get("EMBEDDING_QUERY_PROMPT") or "query: ",
        )
    elif backend == "fake":
        embedder = FakeEmbedder()
    elif backend == "bge_m3":
        embedder = BgeM3Embedder()
    elif backend == "sentence_transformers":
        embedder = SentenceTransformerEmbedder(
            os.environ.get("EMBEDDING_MODEL", ""),
            os.environ.get("EMBEDDING_DEVICE", "cpu"),
            os.environ.get("EMBEDDING_DOCUMENT_PROMPT", ""),
            os.environ.get("EMBEDDING_QUERY_PROMPT", ""),
        )
    else:
        raise ValueError(f"Unsupported EMBEDDING_BACKEND: {backend!r}")
    sparse = os.environ.get("SPARSE_EMBEDDING_BACKEND", "auto")
    if sparse == "auto":
        sparse = "native" if embedder.supports_native_sparse else "bm25"
    if sparse == "native":
        if not embedder.supports_native_sparse:
            raise ValueError("Selected embedding backend has no native sparse vectors; use bm25")
        return embedder
    if sparse == "bm25":
        return SparseOverrideEmbedder(embedder, Bm25Encoder.from_env())
    if sparse == "none":
        return SparseOverrideEmbedder(embedder, None)
    raise ValueError(f"Unsupported SPARSE_EMBEDDING_BACKEND: {sparse!r}")
