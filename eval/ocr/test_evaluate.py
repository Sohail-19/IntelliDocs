"""Metric and runner regressions, using original text and a fake parser."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evaluate import (
    Counts,
    Reference,
    Sample,
    discover,
    distance,
    evaluate,
    page_text,
    score,
)


def result(texts: list[str]) -> Any:
    return SimpleNamespace(
        document=SimpleNamespace(
            pages=[
                SimpleNamespace(
                    page=index,
                    language="hi",
                    blocks=[
                        SimpleNamespace(
                            type="paragraph",
                            text=text,
                            reading_order=0,
                        )
                    ],
                )
                for index, text in enumerate(texts, 1)
            ]
        ),
        parser_name="test",
        parser_version="1",
        warnings=[],
    )


class MetricsTests(unittest.TestCase):
    def test_known_edit_counts(self) -> None:
        self.assertEqual(distance("kitten", "sitting"), 3)
        counts = score("a b c", "a c")
        self.assertEqual(counts.character_edits, 2)
        self.assertEqual(counts.word_edits, 1)
        self.assertAlmostEqual(counts.report()["wer"], 1 / 3)

    def test_unicode_and_whitespace(self) -> None:
        self.assertEqual(score("café\nनमस्ते", "cafe\u0301  नमस्ते").character_edits, 0)
        self.assertGreater(score("A", "a").character_edits, 0)
        self.assertGreater(score("word.", "word").character_edits, 0)

    def test_empty_text_and_insertions(self) -> None:
        self.assertEqual(score("", "").report()["cer"], 0)
        self.assertEqual(score("", "abc").report()["cer"], 3)
        self.assertEqual(score("abc", "").report()["cer"], 1)

    def test_aggregation_is_micro_average(self) -> None:
        counts = Counts(1, 10, 1, 10)
        counts.add(Counts(1, 1, 1, 1))
        self.assertAlmostEqual(counts.report()["cer"], 2 / 11)

    def test_distance_against_simple_oracle(self) -> None:
        # Include repeated tokens where common prefix/suffix trimming can be subtle.
        def oracle(a: str, b: str) -> int:
            matrix = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
            for i in range(len(a) + 1):
                matrix[i][0] = i
            for j in range(len(b) + 1):
                matrix[0][j] = j
            for i in range(1, len(a) + 1):
                for j in range(1, len(b) + 1):
                    matrix[i][j] = min(
                        matrix[i - 1][j] + 1,
                        matrix[i][j - 1] + 1,
                        matrix[i - 1][j - 1] + (a[i - 1] != b[j - 1]),
                    )
            return matrix[-1][-1]

        for a in ("", "a", "ab", "aba", "aaaa", "abba"):
            for b in ("", "a", "ba", "aba", "aaaa", "baba"):
                self.assertEqual(distance(a, b), oracle(a, b))


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = self.root / "original.pdf"
        self.document.write_bytes(b"original test parser input")
        self.text = self.root / "original.txt"
        self.text.write_text("alpha", encoding="utf-8")

    def test_ground_truth_language_is_authoritative(self) -> None:
        sample = Sample(self.document, (Reference("en", self.text),))
        report = evaluate([sample], lambda _path: result(["alpha"]), ["en", "hi"])
        self.assertEqual(report["by_language"]["en"]["metrics"]["cer"], 0)
        self.assertNotIn("hi", report["by_language"])
        self.assertEqual(report["documents"][0]["detected_languages"], {"1": "hi"})

    def test_page_references_and_missing_pages(self) -> None:
        second = self.root / "second.txt"
        second.write_text("beta", encoding="utf-8")
        sample = Sample(
            self.document,
            (Reference("en", self.text, (1,)), Reference("hi", second, (2,))),
        )
        report = evaluate([sample], lambda _path: result(["alpha"]), ["en", "hi"])
        self.assertEqual(report["by_language"]["hi"]["metrics"]["cer"], 1)
        self.assertTrue(report["complete"])

    def test_uncovered_pages_and_failures_are_not_zero_scores(self) -> None:
        sample = Sample(self.document, (Reference("en", self.text, (1,)),))
        report = evaluate([sample], lambda _path: result(["alpha", "extra"]), ["en"])
        self.assertFalse(report["complete"])
        self.assertIsNone(report["metrics"])
        self.assertEqual(report["documents_failed"], 1)

    def test_missing_reference_is_reported(self) -> None:
        sample = Sample(self.document, (Reference("en", self.root / "missing.txt"),))
        report = evaluate([sample], lambda _path: result(["alpha"]), ["en"])
        self.assertEqual(report["documents"][0]["status"], "error")

    def test_partial_success_keeps_failure_coverage(self) -> None:
        missing = self.root / "missing.pdf"
        samples = [
            Sample(self.document, (Reference("en", self.text),)),
            Sample(missing, (Reference("en", self.text),)),
        ]
        report = evaluate(samples, lambda _path: result(["alpha"]), ["en"])
        self.assertEqual(report["documents_scored"], 1)
        self.assertEqual(report["documents_failed"], 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["by_language"]["en"]["documents_total"], 2)

    def test_overlapping_page_references_are_rejected(self) -> None:
        ref = Reference("en", self.text, (1,))
        report = evaluate(
            [Sample(self.document, (ref, ref))], lambda _path: result(["alpha"]), ["en"]
        )
        self.assertEqual(report["documents_failed"], 1)

    def test_table_serialization_and_furniture(self) -> None:
        parsed = result(["alpha"])
        parsed.document.pages[0].blocks.extend(
            [
                SimpleNamespace(
                    type="table",
                    text="do not duplicate",
                    rows=[["one", "two"]],
                    reading_order=1,
                ),
                SimpleNamespace(type="header_footer", text="Page 1", reading_order=2),
            ]
        )
        self.assertEqual(page_text(parsed, False), {1: "alpha\none two"})
        self.assertEqual(page_text(parsed, True), {1: "alpha\none two\nPage 1"})

    def test_paired_folder_discovery_and_manifest(self) -> None:
        docs, truth = self.root / "documents", self.root / "truth"
        (docs / "en").mkdir(parents=True)
        (truth / "en").mkdir(parents=True)
        (docs / "en/form.png").write_bytes(b"image")
        (truth / "en/form.png.txt").write_text("original")
        samples = discover(docs, truth, None)
        self.assertEqual(samples[0].references[0].language, "en")
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "documents": [
                        {
                            "file": "en/form.png",
                            "references": [
                                {
                                    "language": "en",
                                    "text": "en/form.png.txt",
                                    "pages": [1],
                                },
                            ],
                        }
                    ]
                }
            )
        )
        self.assertEqual(discover(docs, truth, manifest)[0].references[0].pages, (1,))


if __name__ == "__main__":
    unittest.main()
