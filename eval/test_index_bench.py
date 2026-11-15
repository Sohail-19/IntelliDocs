"""Benchmark accounting, persistence and failure coverage without models or network."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import index_bench as bench
import pytest
from app.chunking import Chunker, ChunkingConfig
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.models import ParseResult
from qdrant_client import QdrantClient


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("No external network in benchmark tests")

    monkeypatch.setattr(socket.socket, "connect", fail)
    for name, value in {
        "EMBEDDING_BACKEND": "fake",
        "EMBEDDING_BATCH_SIZE": "2",
        "CHUNK_TARGET_TOKENS": "20",
        "CHUNK_OVERLAP_TOKENS": "2",
        "CHUNK_TOKEN_COUNT_METHOD": "whitespace",
    }.items():
        monkeypatch.setenv(name, value)


def sample(identifier: str = "bench-doc") -> ParseResult:
    return ParseResult.model_validate(
        {
            "document": {
                "id": identifier,
                "filename": "sample.txt",
                "mime_type": "text/plain",
                "source_uri": "file:///synthetic",
                "status": "ready",
                "department": "legal",
                "doc_type": "circular",
                "doc_date": "2026-11-15",
                "pages": [
                    {
                            "page": 1,
                        "language": "en",
                        "blocks": [
                            {
                                "type": "paragraph",
                                "text": "Synthetic public fixture.",
                                "bbox": [0, 0, 100, 100],
                                "reading_order": 0,
                            }
                        ],
                    }
                ],
            },
            "parser_name": "synthetic",
            "parser_version": "1",
            "warnings": [],
        }
    )


def test_cold_batches_deduplicates_attachments_and_round_trips_payload(tmp_path: Path) -> None:
    child = sample("attachment")
    child.document.parent_document_id = "bench-doc"
    parent = sample()
    parent.attachments = [child]
    first, second = tmp_path / "parent.json", tmp_path / "child.json"
    first.write_text(parent.model_dump_json(exclude_none=True))
    second.write_text(child.model_dump_json(exclude_none=True))
    client = QdrantClient(":memory:")
    try:
        embedder = bench.MeasuredEmbedder(FakeEmbedder())
        store = QdrantStore(client, "bench", embedder)
        report = bench.index_documents(
            [first, second],
            Chunker(ChunkingConfig(20, 2, "whitespace")),
            embedder,
            store,
            2,
        )
        assert report["complete"]
        assert report["documents"] == report["chunks"] == embedder.chunks == 2
        assert report["duplicate_documents_skipped"] == 1
        assert client.count("bench").count == 2
        for point in client.scroll("bench")[0]:
            assert point.payload and point.payload["department"] == "legal"
            assert point.payload["doc_date"] == "2026-11-15"
        changed = child.model_copy(deep=True)
        changed.document.filename = "different.txt"
        second.write_text(changed.model_dump_json(exclude_none=True))
        conflicting = bench.index_documents(
            [first, second],
            Chunker(ChunkingConfig(20, 2, "whitespace")),
            embedder,
            store,
            2,
        )
        assert not conflicting["complete"]
        assert "Conflicting" in conflicting["errors"][0]["error"]
    finally:
        client.close()


def test_embedding_time_excludes_model_load(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([1.0, 6.0, 10.0, 12.0])
    monkeypatch.setattr(bench, "perf_counter", lambda: next(times))
    embedder = bench.MeasuredEmbedder(FakeEmbedder())
    embedder.load()
    embedder.embed(["one", "two"])
    assert embedder.load_seconds == 5
    assert embedder.embed_seconds == 2
    assert embedder.chunks == 2


def test_cli_persistent_size_and_incomplete_reports(tmp_path: Path) -> None:
    corpus = tmp_path / "input"
    corpus.mkdir()
    (corpus / "parse_result.json").write_text(sample().model_dump_json(exclude_none=True))
    (corpus / "index_cache.json").write_text("not a parse result")
    output, database = tmp_path / "report.json", tmp_path / "qdrant"
    arguments = [
        "--documents",
        str(corpus),
        "--qdrant-path",
        str(database),
        "--collection",
        "benchmark",
        "--output",
        str(output),
    ]
    assert bench.main(arguments) == 0
    report = json.loads(output.read_text())
    assert report["documents"] == report["chunks"] == report["qdrant"]["points"] == 1
    assert report["qdrant"]["size_bytes"] > 0
    assert report["qdrant"]["size_method"] == "local_sqlite_files"
    assert report["peak_rss_bytes"] > 0
    assert report["embedding"]["seconds_per_chunk"] >= 0
    assert len(report["input_files"]) == 1
    assert bench.main(arguments + ["--overwrite"]) == 1  # Refuses the existing collection.
    (corpus / "broken.json").write_text("not JSON")
    output.unlink()
    arguments[arguments.index("benchmark")] = "second_benchmark"
    assert bench.main(arguments) == 1
    assert not json.loads(output.read_text())["complete"]


def test_rss_units(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench.resource, "getrusage", lambda _kind: SimpleNamespace(ru_maxrss=4))
    monkeypatch.setattr(bench.sys, "platform", "linux")
    assert bench.peak_rss_bytes() == 4096
    monkeypatch.setattr(bench.sys, "platform", "darwin")
    assert bench.peak_rss_bytes() == 4


def test_server_size_uses_and_removes_only_its_snapshot() -> None:
    calls: list[str] = []

    class Client:
        def collection_exists(self, _name: str) -> bool:
            return True

        def count(self, _name: str, *, exact: bool) -> SimpleNamespace:
            assert exact
            return SimpleNamespace(count=7)

        def create_snapshot(self, *, collection_name: str, wait: bool) -> SimpleNamespace:
            assert collection_name == "benchmark" and wait
            return SimpleNamespace(name="snapshot", size=1234)

        def delete_snapshot(self, *, collection_name: str, snapshot_name: str, wait: bool) -> None:
            assert collection_name == "benchmark" and wait
            calls.append(snapshot_name)

    result = bench.collection_size(Client(), "benchmark", None)  # type: ignore[arg-type]
    assert result == {"points": 7, "size_bytes": 1234, "size_method": "server_snapshot"}
    assert calls == ["snapshot"]
