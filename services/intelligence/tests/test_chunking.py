"""Exact offsets, structural isolation, and configurable whole-sentence chunk windows."""

import json
from datetime import date
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.chunking import Chunker, ChunkingConfig, canonical_document
from app.chunking.chunker import sentence_spans
from intellidocs_intelligence.chunk_ids import chunk_id
from intellidocs_intelligence.models import Chunk, Page, ParsedDocument, TableBlock, TextBlock


def text_block(text: str, order: int = 0, **fields: object) -> TextBlock:
    return TextBlock.model_validate(
        {
            "type": "paragraph",
            "text": text,
            "reading_order": order,
            "bbox": [0, 0, 100, 20],
            **fields,
        }
    )


def document(*pages: Page) -> ParsedDocument:
    return ParsedDocument(
        id="doc-original",
        filename="original.pdf",
        mime_type="application/pdf",
        source_uri="file:///original.pdf",
        status="ready",
        pages=list(pages),
    )


def page(*blocks: TextBlock | TableBlock, number: int = 1, language: str = "en") -> Page:
    return Page(page=number, language=language, blocks=list(blocks))


def assert_citations(doc: ParsedDocument, chunks: list[Chunk]) -> None:
    canonical = canonical_document(doc)
    schema_path = Path(__file__).resolve().parents[3] / "contracts/schemas/chunk.v1.schema.json"
    validator = Draft202012Validator(
        json.loads(schema_path.read_text()), format_checker=FormatChecker()
    )
    for chunk in chunks:
        assert canonical.text[chunk.char_start : chunk.char_end] == chunk.text
        sources = [
            span
            for span in canonical.blocks
            if span.start < chunk.char_end and span.end > chunk.char_start
        ]
        assert all(span.block.type != "header_footer" for span in sources)
        assert chunk.block_ids == [span.block_id for span in sources]
        assert chunk.page_start == sources[0].page
        assert chunk.page_end == sources[-1].page
        assert chunk.page == chunk.page_start
        assert chunk.chunk_id == chunk.id
        assert chunk.doc_id == doc.id
        validator.validate(chunk.model_dump(mode="json", exclude_none=True))


def test_clause_is_kept_whole_and_metadata_survives() -> None:
    doc = document(
        page(
            text_block(
                "1. Deliver the monthly report.",
                type="clause",
                clause_number="1.",
                heading_path=["Section 3"],
            ),
            text_block("Keep an original copy.", order=1, heading_path=["Section 3"]),
        )
    )
    chunker = Chunker(ChunkingConfig(50, 5, "whitespace"))
    chunks = chunker.chunk(
        doc, department="legal", doc_date=date(2026, 11, 15), doc_type="agreement"
    )
    assert len(chunks) == 1
    assert chunks[0].clause_number == "1."
    assert chunks[0].heading_path == ["Section 3"]
    assert chunks[0].department == "legal"
    assert chunks[0].doc_date == date(2026, 11, 15)
    assert chunks[0].doc_type == "agreement"
    assert chunks == chunker.chunk(
        doc, department="legal", doc_date=date(2026, 11, 15), doc_type="agreement"
    )
    assert_citations(doc, chunks)


def test_long_clause_splits_with_whole_sentence_overlap() -> None:
    sentences = [f"Sentence {index} has words." for index in range(1, 6)]
    doc = document(
        page(text_block("1.1 " + " ".join(sentences), type="clause", clause_number="1.1"))
    )
    chunks = Chunker(ChunkingConfig(9, 4, "whitespace")).chunk(doc)
    assert len(chunks) == 4
    assert chunks[0].text == "1.1 " + " ".join(sentences[:2])
    assert chunks[1].text == " ".join(sentences[1:3])
    for left, right in zip(chunks, chunks[1:], strict=False):
        assert left.char_start < right.char_start < left.char_end < right.char_end
        assert left.text.endswith(right.text.split(".", 1)[0] + ".")
    assert all(chunk.clause_number == "1.1" for chunk in chunks)
    assert_citations(doc, chunks)


def test_single_oversized_sentence_is_not_cut() -> None:
    sentence = (
        "This deliberately long sentence must remain entirely intact despite the token target."
    )
    doc = document(page(text_block(sentence)))
    chunks = Chunker(ChunkingConfig(3, 1, "whitespace")).chunk(doc)
    assert [chunk.text for chunk in chunks] == [sentence]
    assert_citations(doc, chunks)


def test_chunk_identity_replaces_text_at_same_document_offsets() -> None:
    chunker = Chunker(ChunkingConfig(50, 0, "whitespace"))
    first = chunker.chunk(document(page(text_block("Original report."))))[0]
    second = chunker.chunk(document(page(text_block("Modified report."))))[0]
    assert first.text != second.text
    assert first.id == second.id == chunk_id(first.doc_id, first.char_start, first.char_end)


def test_sentence_continuation_across_blocks_is_not_cut() -> None:
    doc = document(
        page(
            text_block("This original sentence continues"),
            text_block("in the next block without losing its ending.", order=1),
        )
    )
    chunks = Chunker(ChunkingConfig(3, 1, "whitespace")).chunk(doc)
    assert len(chunks) == 1
    assert chunks[0].text == canonical_document(doc).text
    assert_citations(doc, chunks)


def test_small_overlap_rounds_to_whole_sentences_and_makes_progress() -> None:
    doc = document(
        page(
            text_block(
                "First sentence has words. Second sentence has words. "
                "Third sentence has words. Fourth sentence has words."
            )
        )
    )
    chunks = Chunker(ChunkingConfig(8, 1, "whitespace")).chunk(doc)
    assert len(chunks) == 3
    assert chunks[0].text.endswith("Second sentence has words.")
    assert chunks[1].text.startswith("Second sentence has words.")
    assert_citations(doc, chunks)


def test_table_is_own_chunk_even_if_oversized() -> None:
    table = TableBlock(
        text="This flattened copy must not appear",
        rows=[
            ["Item", "Count"],
            ["Original reports", "12"],
            ["नोटिस", "3"],
        ],
        bbox=[0, 30, 100, 100],
        reading_order=1,
    )
    doc = document(page(text_block("Before table."), table, text_block("After table.", order=2)))
    chunks = Chunker(ChunkingConfig(2, 1, "whitespace")).chunk(doc)
    assert len(chunks) == 3
    assert chunks[1].text == "Item\tCount\nOriginal reports\t12\nनोटिस\t3"
    assert chunks[1].block_ids == ["doc-original:p1:b1"]
    assert_citations(doc, chunks)


def test_furniture_is_a_hard_boundary_even_for_same_clause() -> None:
    doc = document(
        page(
            text_block("First sentence.", type="clause", clause_number="1."),
            text_block("REPEATED FOOTER", order=1, type="header_footer"),
            text_block("Next sentence.", order=2, clause_number="1."),
        )
    )
    chunks = Chunker(ChunkingConfig(100, 50, "whitespace")).chunk(doc)
    assert [chunk.text for chunk in chunks] == ["First sentence.", "Next sentence."]
    assert "REPEATED FOOTER" in canonical_document(doc).text
    assert_citations(doc, chunks)


def test_clause_and_heading_changes_do_not_share_overlap() -> None:
    doc = document(
        page(
            text_block("Section 3", type="heading", heading_level=1),
            text_block("1. First clause.", order=1, type="clause", clause_number="1."),
            text_block("2. Next clause.", order=2, type="clause", clause_number="2."),
            text_block("3.2 Delivery", order=3, type="heading", heading_level=2),
            text_block("Remaining section text.", order=4),
        )
    )
    chunks = Chunker(ChunkingConfig(100, 20, "whitespace")).chunk(doc)
    assert len(chunks) == 4
    assert chunks[1].heading_path == ["Section 3"]
    assert chunks[1].clause_number == "1."
    assert chunks[2].clause_number == "2."
    assert chunks[3].heading_path == ["Section 3", "3.2 Delivery"]
    assert chunks[3].clause_number is None
    assert_citations(doc, chunks)


def test_exact_cross_page_range_and_unicode_offsets() -> None:
    doc = document(
        page(text_block("Continuation 🙂 with café."), number=3),
        page(text_block("Second page continuation."), number=4),
    )
    chunks = Chunker(ChunkingConfig(100, 5, "unicode_words")).chunk(doc)
    assert len(chunks) == 1
    assert (chunks[0].page_start, chunks[0].page_end) == (3, 4)
    assert chunks[0].block_ids == ["doc-original:p3:b0", "doc-original:p4:b0"]
    assert_citations(doc, chunks)


def test_language_changes_are_boundaries() -> None:
    doc = document(
        page(text_block("Original English text.")),
        page(text_block("यह मूल वाक्य है।"), number=2, language="hi"),
    )
    chunks = Chunker(ChunkingConfig(100, 5, "unicode_words")).chunk(doc)
    assert [chunk.language for chunk in chunks] == ["en", "hi"]
    assert_citations(doc, chunks)


def test_canonical_order_and_ids_do_not_depend_on_input_list_order() -> None:
    first = text_block("First text.", order=1)
    second = text_block("Second text.", order=2)
    doc = document(page(text_block("Last page."), number=2), page(second, first))
    canonical = canonical_document(doc)
    assert canonical.text == "First text.\n\nSecond text.\n\nLast page."
    assert [span.block_id for span in canonical.blocks] == [
        "doc-original:p1:b1",
        "doc-original:p1:b2",
        "doc-original:p2:b0",
    ]
    assert_citations(doc, Chunker(ChunkingConfig(100, 0, "whitespace")).chunk(doc))


def test_paragraph_boundaries_are_preferred() -> None:
    doc = document(
        page(
            text_block("First paragraph ends here."),
            text_block("Second paragraph starts here. Another sentence follows here.", order=1),
        )
    )
    chunks = Chunker(ChunkingConfig(8, 0, "whitespace")).chunk(doc)
    assert chunks[0].text == "First paragraph ends here."
    assert chunks[1].text == "Second paragraph starts here. Another sentence follows here."
    assert_citations(doc, chunks)


def test_sentence_spans_preserve_abbreviations_numbers_and_danda() -> None:
    texts = [
        "Dr. Rao visited the U.S. office. Another report follows.",
        "1.1 Values include 3.14 units. Another report follows.",
        "यह पहला वाक्य है। यह दूसरा वाक्य है।",
    ]
    for text in texts:
        assert len(sentence_spans(text)) == 2


def test_configuration_is_explicit_and_counting_is_selectable() -> None:
    config = ChunkingConfig.from_env(
        {
            "CHUNK_TARGET_TOKENS": "20",
            "CHUNK_OVERLAP_TOKENS": "3",
            "CHUNK_TOKEN_COUNT_METHOD": "whitespace",
        }
    )
    assert config.count("alpha, beta!") == 2
    assert ChunkingConfig(20, 0, "unicode_words").count("alpha, beta!") == 4
    assert ChunkingConfig(20, 0, "characters").count("alpha") == 5
    with pytest.raises(ValueError, match="Missing chunking"):
        ChunkingConfig.from_env({})
    for target, overlap, method in [
        (0, 0, "whitespace"),
        (5, 5, "whitespace"),
        (5, -1, "whitespace"),
        (5, 0, "unknown"),
    ]:
        with pytest.raises(ValueError):
            ChunkingConfig(target, overlap, method)


def test_empty_documents_and_ambiguous_positions() -> None:
    chunker = Chunker(ChunkingConfig(20, 0, "whitespace"))
    assert chunker.chunk(document()) == []
    assert chunker.chunk(document(page(text_block("Header", type="header_footer")))) == []
    with pytest.raises(ValueError, match="Duplicate reading_order"):
        chunker.chunk(document(page(text_block("One."), text_block("Two."))))
    with pytest.raises(ValueError, match="Duplicate page"):
        chunker.chunk(document(page(text_block("One.")), page(text_block("Two."))))
