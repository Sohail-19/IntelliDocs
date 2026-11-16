"""Test reranking without downloading weights or importing the optional extra."""

import socket
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.reranking import (
    CrossEncoderReranker,
    FakeReranker,
    NoOpReranker,
    Reranker,
    RerankerConfig,
    create_reranker,
)
from app.retrieval import Candidate
from intellidocs_intelligence.models import Chunk


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Reranker tests must be offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def candidate(doc_id: str, text: str, score: float) -> Candidate:
    return Candidate(
        chunk=Chunk(id=doc_id, doc_id=doc_id, page=1, char_start=0, char_end=len(text), text=text),
        score=score,
        source="fused",
    )


@pytest.fixture
def candidates() -> list[Candidate]:
    return [
        candidate("unrelated", "Lunch at noon", 0.9),
        candidate("partial", "REPORT", 0.8),
        candidate("match", "विभाग Report " + "long " * 25, 0.7),
        candidate("tied", "report विभाग", 0.6),
        candidate("last", "Dinner tonight", 0.5),
    ]


def test_fake_reorders_stably_and_preserves_original_candidates(
    candidates: list[Candidate],
) -> None:
    reranker = FakeReranker()
    result = reranker.rerank("report विभाग", candidates, top_n=3)
    assert result == [
        replace(candidates[2], rerank_score=2.0),
        replace(candidates[3], rerank_score=2.0),
        replace(candidates[1], rerank_score=1.0),
    ]
    assert reranker.rerank("report विभाग", candidates, top_n=3) == result
    assert all(item.rerank_score is None for item in candidates)


def test_noop_keeps_original_order_and_scores(candidates: list[Candidate]) -> None:
    candidates[0] = replace(candidates[0], rerank_score=0.1)
    reranker = NoOpReranker()
    result = reranker.rerank("report विभाग", candidates, top_n=3)
    assert result == candidates[:3]
    assert all(output is original for output, original in zip(result, candidates, strict=False))
    assert reranker.rerank("report", candidates, top_n=10) == candidates


@pytest.mark.parametrize(
    "reranker", [NoOpReranker(), FakeReranker(), CrossEncoderReranker(RerankerConfig())]
)
def test_empty_and_invalid_limits(reranker: Reranker, candidates: list[Candidate]) -> None:
    assert reranker.rerank("query", [], top_n=2) == []
    assert reranker.rerank("query", candidates, top_n=0) == []
    for top_n in (-1, True):
        with pytest.raises(ValueError, match="top_n"):
            reranker.rerank("query", candidates, top_n=top_n)


def test_factory_selects_backend_and_reads_model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert RerankerConfig.from_env({}).model_name == "BAAI/bge-reranker-v2-m3"
    for backend, expected in [("none", NoOpReranker), ("fake", FakeReranker)]:
        monkeypatch.setenv("RERANKER_BACKEND", backend)
        assert isinstance(create_reranker(), expected)
    for name, value in {
        "RERANKER_BACKEND": "cross_encoder",
        "RERANKER_MODEL": "/models/multilingual",
        "RERANKER_DEVICE": "cuda:0",
        "RERANKER_BATCH_SIZE": "3",
        "RERANKER_MAX_LENGTH": "128",
    }.items():
        monkeypatch.setenv(name, value)
    reranker = create_reranker()
    assert isinstance(reranker, CrossEncoderReranker)
    assert reranker.config == RerankerConfig(
        "cross_encoder", "/models/multilingual", "cuda:0", 3, 128
    )
    monkeypatch.setenv("RERANKER_BATCH_SIZE", "0")
    with pytest.raises(ValueError, match="RERANKER_BATCH_SIZE"):
        create_reranker()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    def tokenize_batch(queries: list[str], passages: list[str], **options: object) -> Mock:
        assert options["truncation"] == "longest_first"
        max_length = options["max_length"]
        assert isinstance(max_length, int)
        sequences: list[tuple[list[str], list[str]]] = []
        for query, passage in zip(queries, passages, strict=True):
            query_tokens, passage_tokens = query.casefold().split(), passage.casefold().split()
            while len(query_tokens) + len(passage_tokens) + 4 > max_length:
                longer = query_tokens if len(query_tokens) > len(passage_tokens) else passage_tokens
                longer.pop()
            sequences.append((query_tokens, passage_tokens))
        inputs = Mock()
        inputs.to.return_value = {"pairs": sequences}
        return inputs

    def inference(
        *, pairs: list[tuple[list[str], list[str]]], return_dict: bool
    ) -> SimpleNamespace:
        assert return_dict
        logits = Mock()
        logits.reshape.return_value.float.return_value.tolist.return_value = [
            float(len(set(query) & set(passage))) - 1.0 for query, passage in pairs
        ]
        return SimpleNamespace(logits=logits)

    tokenizer = Mock(
        side_effect=tokenize_batch,
        model_max_length=10,
        num_special_tokens_to_add=Mock(return_value=4),
    )
    model = Mock(side_effect=inference, config=SimpleNamespace(num_labels=1))
    load_tokenizer, load_model = Mock(return_value=tokenizer), Mock(return_value=model)
    inference_mode = Mock(side_effect=nullcontext)
    imports = Mock(
        side_effect={
            "torch": SimpleNamespace(inference_mode=inference_mode),
            "transformers": SimpleNamespace(
                AutoTokenizer=SimpleNamespace(from_pretrained=load_tokenizer),
                AutoModelForSequenceClassification=SimpleNamespace(from_pretrained=load_model),
            ),
        }.__getitem__
    )
    monkeypatch.setattr("app.reranking.import_module", imports)
    return SimpleNamespace(
        imports=imports,
        tokenizer=tokenizer,
        model=model,
        load_tokenizer=load_tokenizer,
        load_model=load_model,
        inference_mode=inference_mode,
    )


def test_cross_encoder_loads_on_first_nonempty_call_and_reuses_model(
    backend: SimpleNamespace,
    candidates: list[Candidate],
) -> None:
    config = RerankerConfig(model_name="local-model", device="mps", batch_size=2, max_length=12)
    reranker = CrossEncoderReranker(config)
    backend.imports.assert_not_called()
    assert reranker.rerank("query", [], top_n=3) == []
    assert reranker.rerank("query", candidates, top_n=0) == []
    backend.imports.assert_not_called()
    result = reranker.rerank("report विभाग", candidates, top_n=4)
    assert result == [
        replace(candidates[2], rerank_score=1.0),
        replace(candidates[3], rerank_score=1.0),
        replace(candidates[1], rerank_score=0.0),
        replace(candidates[0], rerank_score=-1.0),
    ]
    for loader in (backend.load_model, backend.load_tokenizer):
        loader.assert_called_once_with(
            "local-model", local_files_only=True, trust_remote_code=False
        )
    backend.model.to.assert_called_once_with("mps")
    backend.model.eval.assert_called_once()
    assert backend.inference_mode.call_count == 3
    calls = backend.tokenizer.call_args_list
    assert [len(call.args[1]) for call in calls] == [2, 2, 1]
    assert calls[1].args[1][0] == candidates[2].chunk.text
    assert all(
        call.kwargs
        == {
            "padding": True,
            "truncation": "longest_first",
            "max_length": 10,
            "return_tensors": "pt",
        }
        for call in calls
    )
    assert reranker.rerank("report विभाग", candidates, top_n=4) == result
    assert backend.load_model.call_count == backend.load_tokenizer.call_count == 1


def test_batched_scores_match_one_big_batch(
    backend: SimpleNamespace,
    candidates: list[Candidate],
) -> None:
    query = "report विभाग " + "long " * 50
    small = CrossEncoderReranker(RerankerConfig(batch_size=2, max_length=8))
    large = CrossEncoderReranker(RerankerConfig(batch_size=16, max_length=8))
    batched = small.rerank(query, candidates, top_n=len(candidates))
    assert batched == large.rerank(query, candidates, top_n=len(candidates))
    assert [len(call.args[1]) for call in backend.tokenizer.call_args_list] == [2, 2, 1, 5]
    for call in backend.model.call_args_list:
        for query_tokens, passage_tokens in call.kwargs["pairs"]:
            assert len(query_tokens) + len(passage_tokens) + 4 <= 8


def test_too_small_length_fails_before_model_load(
    backend: SimpleNamespace,
    candidates: list[Candidate],
) -> None:
    with pytest.raises(ValueError, match="special tokens"):
        CrossEncoderReranker(RerankerConfig(max_length=4)).rerank("query", candidates, 2)
    backend.load_model.assert_not_called()


def test_missing_extra_fails_only_at_inference(
    monkeypatch: pytest.MonkeyPatch,
    candidates: list[Candidate],
) -> None:
    monkeypatch.setattr("app.reranking.import_module", Mock(side_effect=ImportError))
    reranker = CrossEncoderReranker(RerankerConfig())
    with pytest.raises(RuntimeError, match=r"\[reranker\] extra"):
        reranker.rerank("query", candidates, top_n=1)
