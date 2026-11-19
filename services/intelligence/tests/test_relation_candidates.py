"""Look up prior documents against Qdrant's in-memory index."""

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient

from app.relations.candidates import CandidateFinder, CandidateFinderConfig
from app.retrieval import RetrievalConfig
from app.vector_store import QdrantStore
from intellidocs_intelligence.chunk_ids import chunk_id
from intellidocs_intelligence.embeddings import FakeEmbedder
from intellidocs_intelligence.models import Chunk, OrgConfig

SOURCE_DAY = date(2026, 10, 15)
TARGET_DAY = date(2025, 3, 12)


@pytest.fixture
def store() -> Iterator[QdrantStore]:
    client = QdrantClient(":memory:")
    try:
        yield QdrantStore(client, "relations", FakeEmbedder())
    finally:
        client.close()


@pytest.fixture
def org() -> OrgConfig:
    path = Path(__file__).resolve().parents[3] / "config/orgs/example.yaml"
    return OrgConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def chunk(doc_id: str, text: str, *, start: int = 0, **metadata: object) -> Chunk:
    end = start + len(text)
    return Chunk.model_validate(
        {
            "doc_id": doc_id,
            "id": chunk_id(doc_id, start, end),
            "page": 1,
            "char_start": start,
            "char_end": end,
            "text": text,
            "org_id": "example",
            "doc_type": "agreement",
            "language": "en",
            "doc_date": TARGET_DAY,
            **metadata,
        }
    )


@pytest.mark.parametrize(
    "language, source_text, target_text, key",
    [
        (
            "en",
            "In supersession of circular No. CIR-24/117, file quarterly.",
            "Circular CIR-24/117\nPrevious filing rules",
            "number:cir-24/117",
        ),
        (
            "hi",
            "आदेश संख्या सर्क-२४/११७ के संशोधन में नया निर्देश।",
            "आदेश सर्क-24/117\nपुराना निर्देश",
            "number:सर्क-24/117",
        ),
        (
            "en",
            "Proceed vide the Municipal Filing Manual until June.",
            "Municipal Filing Manual\nPrevious filing rules",
            "title:municipal filing manual",
        ),
        (
            "hi",
            "आदेश दिनांक १२ मार्च २०२५ के संदर्भ में।",
            "पुरानी समय सीमा",
            "date:2025-03-12",
        ),
    ],
)
def test_reference_is_found_independently_of_similarity(
    store: QdrantStore,
    org: OrgConfig,
    language: str,
    source_text: str,
    target_text: str,
    key: str,
) -> None:
    source = chunk("new", source_text, language=language, doc_date=SOURCE_DAY, doc_type=None)
    target = chunk("old", target_text, language=language)
    store.upsert_chunks([source, target])
    result = CandidateFinder(store, org).find("new", org_id="example")
    assert [candidate.target_doc_id for candidate in result] == ["old"]
    (pair,) = result[0].matching_pairs
    assert pair.source_chunk == source and pair.target_chunk == target
    assert pair.reasons == ("explicit_reference",)
    assert key in pair.evidence_keys
    assert not pair.needs_date_review


def test_similarity_only_uses_related_types_and_filters_before_the_hit_cutoff(
    store: QdrantStore, org: OrgConfig
) -> None:
    text = "Freight costs are allocated among regional partners"
    source = chunk("new", text, doc_date=SOURCE_DAY)
    earlier = chunk("early", text, doc_type="invoice")
    store.upsert_chunks(
        [
            source,
            earlier,
            chunk("later", text, doc_date=date(2026, 10, 16)),
            chunk("same-day", text, doc_date=SOURCE_DAY),
            chunk("other-org", text, org_id="elsewhere"),
            chunk("unrelated-type", text, doc_type="unconfigured"),
        ]
    )
    finder = CandidateFinder(store, org, CandidateFinderConfig(chunk_hits=1))
    result = finder.find(source.doc_id, org_id="example")
    assert [candidate.target_doc_id for candidate in result] == ["early"]
    (pair,) = result[0].matching_pairs
    assert pair.target_chunk == earlier
    assert pair.reasons == ("similarity",)
    assert pair.similarity_score is not None and pair.similarity_score > 0
    assert not pair.needs_date_review
    assert finder.find(source.doc_id, org_id="elsewhere") == []


@pytest.mark.parametrize(
    "source_date, target_date",
    [(SOURCE_DAY, None), (None, TARGET_DAY), (None, None)],
)
def test_missing_date_flags_the_pair_and_document(
    store: QdrantStore, org: OrgConfig, source_date: date | None, target_date: date | None
) -> None:
    text = "Settlements follow the regional schedule"
    store.upsert_chunks(
        [
            chunk("new", text, doc_date=source_date),
            chunk("needs-review", text, doc_date=target_date),
        ]
    )
    (result,) = CandidateFinder(store, org).find("new", org_id="example")
    assert result.needs_date_review
    assert result.matching_pairs[0].needs_date_review


def test_shared_structure_and_reference_budgets_are_independent_of_similarity(
    store: QdrantStore, org: OrgConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = chunk(
        "new",
        "New rules",
        clause_number="4.2.",
        heading_path=["Audit   Procedure"],
        doc_date=SOURCE_DAY,
        doc_type=None,
    )
    earlier = [
        chunk(f"old-{index}", "Old rules", clause_number="4.2", heading_path=["audit procedure"])
        for index in range(4)
    ]
    store.upsert_chunks([source, *earlier])
    embed = Mock(wraps=store.embedder.embed_queries)
    monkeypatch.setattr(store.embedder, "embed_queries", embed)
    config = CandidateFinderConfig.from_env({"RELATION_CANDIDATE_LIMIT": "2"})
    finder = CandidateFinder(store, org, config)
    result = finder.find("new", org_id="example")
    assert len(result) == 2
    assert [item.reasons for item in result] == [("shared_clause", "shared_heading")] * 2
    assert result == finder.find("new", org_id="example")
    embed.assert_not_called()


def test_a_late_reference_is_prioritised_and_lookup_matches_merge(
    store: QdrantStore, org: OrgConfig
) -> None:
    title = "Facility Procedures"
    source = chunk("new", "First page introduction", doc_date=SOURCE_DAY)
    reference = chunk("new", f'Vide "{title}".', start=50, doc_date=SOURCE_DAY, clause_number="5")
    target = chunk("old", "Earlier section without a title", clause_number="5")
    store.upsert_chunks([source, reference])
    store.upsert_chunks([target], document_title=title)
    finder = CandidateFinder(
        store, org, CandidateFinderConfig(query_chunks=1), RetrievalConfig(mode="sparse")
    )
    (result,) = finder.find("new", org_id="example")
    (pair,) = result.matching_pairs
    assert pair.source_chunk == reference
    assert pair.reasons == ("explicit_reference", "shared_clause")
