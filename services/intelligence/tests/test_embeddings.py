"""Dense/sparse adapter tests without network calls or model downloads."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from intellidocs_intelligence.embeddings import (
    BgeM3Embedder,
    FakeEmbedder,
    SentenceTransformerEmbedder,
    embedder_from_env,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Embedding tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", fail)


def test_fake_is_deterministic_and_json_serializable() -> None:
    first = FakeEmbedder().embed(["original report", "नमस्ते", ""])
    assert first == FakeEmbedder().embed(["original report", "नमस्ते", ""])
    assert len(first) == 3
    assert len(first[0]["dense"]) == 16
    assert first[0] != first[1]
    assert first[2]["dense"] == [0.0] * 16
    assert first[2]["sparse"] == {"indices": [], "values": []}
    assert FakeEmbedder().embed([]) == []
    json.dumps(first, allow_nan=False)


def test_fake_is_stable_across_python_hash_seeds() -> None:
    code = (
        "import json; from intellidocs_intelligence.embeddings import FakeEmbedder; "
        "print(json.dumps(FakeEmbedder().embed(['original repeated repeated'])))"
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code], env={**os.environ, "PYTHONHASHSEED": seed}
        )
        for seed in ("1", "42")
    ]
    assert outputs[0] == outputs[1]


def test_fake_sparse_indices_are_unique_sorted_and_counted() -> None:
    sparse = FakeEmbedder().embed(["repeat repeat other"])[0]["sparse"]
    assert sparse["indices"] == sorted(set(sparse["indices"]))
    assert sorted(sparse["values"]) == [1.0, 2.0]


def test_lazy_constructor_and_empty_batch_do_not_import_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_name: str) -> None:
        raise AssertionError("Heavy dependency imported before embed")

    monkeypatch.setattr("intellidocs_intelligence.embeddings.import_module", fail)
    embedder = BgeM3Embedder("configured-model", "cpu")
    assert embedder.embed([]) == []


def test_bge_loads_once_and_adapts_dense_sparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    encodes: list[list[str]] = []

    class Model:
        def __init__(self, source: str, **kwargs: Any) -> None:
            calls.append({"source": source, **kwargs})

        def encode(self, texts: list[str], **kwargs: Any) -> dict[str, Any]:
            assert kwargs == {
                "return_dense": True,
                "return_sparse": True,
                "return_colbert_vecs": False,
            }
            encodes.append(texts)
            return {
                "dense_vecs": [[0.25, 0.75] for _ in texts],
                "lexical_weights": [{"10": 0.2, "2": 0.8} for _ in texts],
            }

    def load(name: str) -> Any:
        assert name == "FlagEmbedding"
        return SimpleNamespace(BGEM3FlagModel=Model)

    monkeypatch.setenv("EMBEDDING_MODEL", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DEVICE", "cpu")
    monkeypatch.setattr("intellidocs_intelligence.embeddings.import_module", load)
    embedder = BgeM3Embedder()
    assert not calls
    embedder.load()
    vectors = embedder.embed(["one", "two"])
    embedder.embed(["three"])
    assert len(calls) == 1
    assert calls[0] == {
        "source": str(tmp_path),
        "devices": "cpu",
        "use_fp16": False,
        "normalize_embeddings": True,
        "trust_remote_code": False,
    }
    assert len(encodes) == 2
    assert vectors[0] == {
        "dense": [0.25, 0.75],
        "sparse": {"indices": [2, 10], "values": [0.8, 0.2]},
    }
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_missing_cached_model_fails_without_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def snapshot(**kwargs: Any) -> None:
        assert kwargs == {"repo_id": "configured/not-cached", "local_files_only": True}
        raise FileNotFoundError("No cached weights")

    def load(name: str) -> Any:
        assert name == "huggingface_hub"
        return SimpleNamespace(snapshot_download=snapshot)

    monkeypatch.setattr("intellidocs_intelligence.embeddings.import_module", load)
    with pytest.raises(RuntimeError, match="not available locally"):
        BgeM3Embedder("configured/not-cached").embed(["text"])


def test_missing_optional_extra_has_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def load(_name: str) -> None:
        raise ImportError("FlagEmbedding is not installed")

    monkeypatch.setattr("intellidocs_intelligence.embeddings.import_module", load)
    with pytest.raises(RuntimeError, match=r"\[bge-m3\]"):
        BgeM3Embedder(str(tmp_path)).embed(["text"])


@pytest.mark.parametrize(
    "output",
    [
        {"dense_vecs": [], "lexical_weights": []},
        {"dense_vecs": [[float("nan")]], "lexical_weights": [{}]},
        {"dense_vecs": [[1.0]], "lexical_weights": [{"-1": 0.5}]},
        {"dense_vecs": [[1.0]], "lexical_weights": [{"1": float("inf")}]},
    ],
)
def test_rejects_invalid_model_outputs(
    output: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    model = SimpleNamespace(encode=lambda *_args, **_kwargs: output)
    monkeypatch.setattr(BgeM3Embedder, "_load", lambda _self: model)
    with pytest.raises(ValueError):
        BgeM3Embedder().embed(["text"])


def test_factory_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    assert isinstance(embedder_from_env(), FakeEmbedder)
    monkeypatch.setenv("EMBEDDING_BACKEND", "bge_m3")
    monkeypatch.setenv("EMBEDDING_MODEL", "chosen-model")
    monkeypatch.setenv("EMBEDDING_DEVICE", "cuda:1")
    embedder = embedder_from_env()
    assert isinstance(embedder, BgeM3Embedder)
    assert embedder.model_name == "chosen-model"
    assert embedder.device == "cuda:1"
    monkeypatch.setenv("EMBEDDING_BACKEND", "sentence_transformers")
    monkeypatch.setenv("EMBEDDING_DOCUMENT_PROMPT", "passage: ")
    smaller = embedder_from_env()
    assert isinstance(smaller, SentenceTransformerEmbedder)
    assert smaller.model_name == "chosen-model" and smaller.device == "cuda:1"
    assert smaller.prompt == "passage: "
    monkeypatch.setenv("EMBEDDING_BACKEND", "unknown")
    with pytest.raises(ValueError, match="Unsupported EMBEDDING_BACKEND"):
        embedder_from_env()


def test_sentence_transformer_loads_once_offline_and_applies_explicit_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[dict[str, Any]] = []
    encodes: list[dict[str, Any]] = []

    class Model:
        def __init__(self, source: str, **kwargs: Any) -> None:
            loads.append({"source": source, **kwargs})

        def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
            encodes.append({"texts": texts, **kwargs})
            return [[0.6, 0.8] for _ in texts]

    def load(name: str) -> Any:
        assert name == "sentence_transformers"
        return SimpleNamespace(SentenceTransformer=Model)

    monkeypatch.setattr("intellidocs_intelligence.embeddings.import_module", load)
    embedder = SentenceTransformerEmbedder("local-small-model", "cpu", "passage: ")
    assert embedder.embed([]) == [] and not loads
    embedder.load()
    assert embedder.embed(["document text"])[0] == {
        "dense": [0.6, 0.8],
        "sparse": {"indices": [], "values": []},
    }
    embedder.embed(["another document"])
    assert loads == [
        {
            "source": "local-small-model",
            "device": "cpu",
            "local_files_only": True,
            "trust_remote_code": False,
        }
    ]
    assert encodes[0] == {
        "texts": ["document text"],
        "prompt": "passage: ",
        "normalize_embeddings": True,
        "convert_to_numpy": True,
        "show_progress_bar": False,
    }
    assert (
        embedder.cache_key()
        != SentenceTransformerEmbedder(
            "local-small-model",
            "cpu",
            "query: ",
        ).cache_key()
    )


@pytest.mark.parametrize("missing_dependency", [True, False])
def test_sentence_transformer_offline_setup_errors(
    monkeypatch: pytest.MonkeyPatch,
    missing_dependency: bool,
) -> None:
    def load(_name: str) -> Any:
        if missing_dependency:
            raise ImportError("Missing library")

        def model(*_args: Any, **kwargs: Any) -> None:
            assert kwargs["local_files_only"] and not kwargs["trust_remote_code"]
            raise FileNotFoundError("Missing provisioned weights")

        return SimpleNamespace(SentenceTransformer=model)

    monkeypatch.setattr("intellidocs_intelligence.embeddings.import_module", load)
    match = "extra" if missing_dependency else "downloads are disabled"
    with pytest.raises(RuntimeError, match=match):
        SentenceTransformerEmbedder("not-cached", "cpu").load()


@pytest.mark.parametrize("output", [[], [[]], [[float("nan")]], [[1.0], [1.0, 2.0]]])
def test_sentence_transformer_validates_outputs(
    monkeypatch: pytest.MonkeyPatch,
    output: list[list[float]],
) -> None:
    model = SimpleNamespace(encode=lambda *_args, **_kwargs: output)
    embedder = SentenceTransformerEmbedder("local", "cpu")
    embedder._model = model
    with pytest.raises(ValueError):
        embedder.embed(["one", "two"] if len(output) == 2 else ["one"])
