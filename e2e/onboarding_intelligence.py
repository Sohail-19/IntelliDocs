"""Local Intelligence process for the cross-service organization onboarding test."""

import json
import socket
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import uvicorn
from qdrant_client import QdrantClient

from app.chunking import Chunker, ChunkingConfig
from app.extraction import CandidateDetector, ObligationExtractor
from app.extraction.service import ExtractionService
from app.indexing import IndexingService
from app.ingestion import storage_from_env
from app.relations.classifier import RelationClassifier
from app.relations.service import RelationService
from app.reranking import NoOpReranker
from app.retrieval import create_retriever
from app.search import SearchService
from app.summarization import SummaryService
from app.summarization.generator import SummaryGenerator
from app.vector_store import QdrantStore
from app.verification import CitationVerifier
from intellidocs_intelligence.cache import DiskCache
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.main import (
    app,
    get_extraction_factory,
    get_indexing_factory,
    get_relation_factory,
    get_search_factory,
    get_summary_factory,
)

FIXTURES = json.loads(Path(__file__).with_name("onboarding-fixtures.json").read_text("utf-8"))


class LocalProvider(FakeProvider):
    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if "\nCHUNKS_JSON\n" in prompt:
            chunks = json.loads(prompt.split("\nCHUNKS_JSON\n", 1)[1])
            return {
                "obligations": [
                    {
                        "chunk_id": chunk["chunk_id"],
                        "action": fixture["action"],
                        "owner_text": fixture["owner"],
                        "deadline_text": fixture["deadline"],
                        "condition": None,
                        "obligation_type": "reporting",
                        "quote": fixture["quote"],
                    }
                    for chunk in chunks
                    for fixture in FIXTURES.values()
                    if fixture["quote"] in chunk["text"]
                ]
            }
        if "\nPAIR_JSON\n" in prompt:
            return {"relation_type": None, "scope": None, "source_quote": "", "target_quote": ""}
        if "\nREQUEST_JSON\n" in prompt:
            context = json.loads(prompt.split("\nREQUEST_JSON\n", 1)[1])
            fixture = FIXTURES["example2"]
            if context.get("role") == "resident_liaison":
                return {
                    "points": [
                        {
                            "chunk_id": chunk["chunk_id"],
                            "quote": fixture["additional_quote"],
                            "text": fixture["additional_point"],
                        }
                        for chunk in context["chunks"]
                        if fixture["additional_quote"] in chunk["text"]
                    ]
                }
            return {"points": []}
        raise AssertionError("Unexpected fixture model call")


def main() -> None:
    storage = storage_from_env()
    embedder = FakeEmbedder()
    provider = LocalProvider(model="onboarding-fixture")
    client = QdrantClient(":memory:")
    try:
        store = QdrantStore(client, "onboarding", embedder)
        indexer = IndexingService(
            storage, Chunker(ChunkingConfig(32, 0, "whitespace")), embedder, store, 4
        )
        extraction = ExtractionService(
            storage, store, lambda org: ObligationExtractor(CandidateDetector(org), provider)
        )
        relations = RelationService(storage, store, lambda org: RelationClassifier(provider, org))
        search = SearchService(create_retriever(store), NoOpReranker(), storage)
        summary = SummaryService(
            storage,
            create_retriever(store),
            SummaryGenerator(provider),
            CitationVerifier(),
            DiskCache(storage.root / "summaries"),
        )
        app.dependency_overrides[get_indexing_factory] = lambda: lambda: nullcontext(indexer)
        app.dependency_overrides[get_extraction_factory] = lambda: lambda: nullcontext(extraction)
        app.dependency_overrides[get_relation_factory] = lambda: lambda: nullcontext(relations)
        app.dependency_overrides[get_search_factory] = lambda: lambda: nullcontext(search)
        app.dependency_overrides[get_summary_factory] = lambda: lambda: nullcontext(summary)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            print(listener.getsockname()[1], flush=True)
            uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(
                sockets=[listener]
            )
    finally:
        client.close()


if __name__ == "__main__":
    main()
