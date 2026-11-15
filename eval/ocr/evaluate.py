"""Offline OCR evaluation against independent, UTF-8 ground truth."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from intellidocs_intelligence.models import ParseResult

ROOT = Path(__file__).resolve().parents[2]
EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".docx"}


def normalize(text: str) -> str:
    """NFC code points; collapse whitespace; preserve case and punctuation."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Exact Levenshtein edits, with linear working memory."""
    start = 0
    end_a, end_b = len(reference), len(hypothesis)
    while start < min(end_a, end_b) and reference[start] == hypothesis[start]:
        start += 1
    while min(end_a, end_b) > start and reference[end_a - 1] == hypothesis[end_b - 1]:
        end_a -= 1
        end_b -= 1
    a, b = reference[start:end_a], hypothesis[start:end_b]
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for row, token in enumerate(a, 1):
        current = [row]
        for column, other in enumerate(b, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (token != other),
                )
            )
        previous = current
    return previous[-1]


@dataclass
class Counts:
    character_edits: int = 0
    reference_characters: int = 0
    word_edits: int = 0
    reference_words: int = 0

    def add(self, other: Counts) -> None:
        for field in asdict(self):
            setattr(self, field, getattr(self, field) + getattr(other, field))

    def report(self) -> dict[str, int | float]:
        return {
            **asdict(self),
            "cer": self.character_edits / max(1, self.reference_characters),
            "wer": self.word_edits / max(1, self.reference_words),
        }


def score(reference: str, hypothesis: str) -> Counts:
    reference, hypothesis = normalize(reference), normalize(hypothesis)
    words, predicted_words = reference.split(), hypothesis.split()
    return Counts(
        distance(reference, hypothesis),
        len(reference),
        distance(words, predicted_words),
        len(words),
    )


@dataclass(frozen=True)
class Reference:
    language: str
    text: Path
    pages: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Sample:
    document: Path
    references: tuple[Reference, ...]


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes dataset root: {relative}")
    return path


def discover(documents: Path, truth: Path, manifest: Path | None) -> list[Sample]:
    if manifest:
        records = json.loads(manifest.read_text(encoding="utf-8"))["documents"]
        samples = []
        for record in records:
            references = []
            for item in record["references"]:
                pages = item.get("pages")
                if pages is not None and (
                    not pages or any(type(p) is not int or p < 1 for p in pages)
                ):
                    raise ValueError(
                        "Reference pages must be a nonempty list of positive integers"
                    )
                references.append(
                    Reference(
                        item["language"],
                        inside(truth, item["text"]),
                        tuple(pages) if pages is not None else None,
                    )
                )
            samples.append(Sample(inside(documents, record["file"]), tuple(references)))
    else:
        samples = []
        for path in sorted(documents.rglob("*")):
            if path.is_file() and path.suffix.lower() in EXTENSIONS:
                relative = path.relative_to(documents)
                if len(relative.parts) < 2:
                    raise ValueError(
                        "Paired folders require documents/<language>/<name>.<ext>"
                    )
                # Retain extension to avoid collisions between name.pdf and name.png.
                paired_refs = (Reference(relative.parts[0], truth / f"{relative}.txt"),)
                samples.append(Sample(path.resolve(), paired_refs))
    if not samples:
        raise ValueError("No evaluation documents found")
    if len({sample.document for sample in samples}) != len(samples):
        raise ValueError("Duplicate document entries in manifest")
    return samples


def page_text(result: ParseResult, include_furniture: bool) -> dict[int, str]:
    output = {}
    for page in sorted(result.document.pages, key=lambda page: page.page):
        if page.page in output:
            raise ValueError(f"Duplicate parsed page number: {page.page}")
        parts: list[str] = []
        for block in sorted(page.blocks, key=lambda block: block.reading_order):
            if block.type == "header_footer" and not include_furniture:
                continue
            if block.type == "table":
                parts.extend(" ".join(row) for row in (block.rows or []))
            else:
                parts.append(block.text)
        output[page.page] = "\n".join(parts)
    return output


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(
    samples: list[Sample],
    parse: Callable[[Path], ParseResult],
    languages: Sequence[str],
    *,
    include_furniture: bool = True,
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    totals = Counts()
    language_counts: dict[str, Counts] = {}
    expected: dict[str, int] = {}
    completed: dict[str, int] = {}
    for sample in samples:
        labels = {ref.language for ref in sample.references}
        for language in labels:
            if not set(language.split("+")) <= set(languages):
                raise ValueError(
                    f"Reference language {language!r} is absent from org config"
                )
            expected[language] = expected.get(language, 0) + 1
            language_counts.setdefault(language, Counts())
        started = perf_counter()
        record: dict[str, Any] = {
            "document": str(sample.document),
            "languages": sorted(labels),
        }
        try:
            if sample.document.suffix.lower() not in EXTENSIONS:
                raise ValueError("Unsupported evaluation document extension")
            if not sample.references:
                raise ValueError("Document has no ground-truth references")
            record["document_sha256"] = digest(sample.document)
            reference_texts = [
                ref.text.read_text(encoding="utf-8") for ref in sample.references
            ]
            record["references"] = [
                {
                    "path": str(ref.text),
                    "sha256": digest(ref.text),
                    "language": ref.language,
                    "pages": ref.pages,
                }
                for ref in sample.references
            ]
            result = parse(sample.document)
            pages = page_text(result, include_furniture)
            selected: set[int] = set()
            doc_counts = Counts()
            per_language: dict[str, Counts] = {}
            for ref, text in zip(sample.references, reference_texts, strict=True):
                numbers = ref.pages if ref.pages is not None else tuple(pages)
                if len(set(numbers)) != len(numbers) or selected.intersection(numbers):
                    raise ValueError("Ground-truth page selections overlap")
                selected.update(numbers)
                # Missing parsed pages count as deletions, not a missing evaluation.
                counts = score(
                    text, "\n".join(pages.get(number, "") for number in numbers)
                )
                per_language.setdefault(ref.language, Counts()).add(counts)
                doc_counts.add(counts)
            if set(pages) - selected:
                raise ValueError(
                    "Parsed pages lack ground truth; provide complete page coverage"
                )
            record.update(
                status="ok",
                metrics=doc_counts.report(),
                by_language={
                    key: value.report() for key, value in per_language.items()
                },
                detected_languages={
                    str(p.page): p.language for p in result.document.pages
                },
                parser_name=result.parser_name,
                parser_version=result.parser_version,
                warnings=result.warnings,
            )
            totals.add(doc_counts)
            for language, counts in per_language.items():
                language_counts[language].add(counts)
                completed[language] = completed.get(language, 0) + 1
        except Exception as error:  # noqa: BLE001 - retain failed samples in the report.
            record.update(status="error", error=f"{type(error).__name__}: {error}")
        record["seconds"] = round(perf_counter() - started, 6)
        documents.append(record)
    succeeded = sum(doc["status"] == "ok" for doc in documents)
    return {
        "complete": succeeded == len(documents),
        "documents_total": len(documents),
        "documents_scored": succeeded,
        "documents_failed": len(documents) - succeeded,
        "metrics": totals.report() if succeeded else None,
        "by_language": {
            language: {
                "documents_scored": completed.get(language, 0),
                "documents_total": expected[language],
                "metrics": counts.report() if completed.get(language, 0) else None,
            }
            for language, counts in sorted(language_counts.items())
        },
        "documents": documents,
    }


def summary(report: dict[str, Any]) -> str:
    width = max(
        40,
        *(len(str(document["document"])) for document in report["documents"]),
        *(len(f"language: {language}") for language in report["by_language"]),
    )
    lines = [f"{'Scope / document':<{width}} Scored  CER (%)  WER (%)"]

    def row(name: str, coverage: str, metrics: dict[str, Any] | None) -> None:
        rates = (
            f"{100 * metrics['cer']:8.2f} {100 * metrics['wer']:8.2f}"
            if metrics
            else "     N/A      N/A"
        )
        lines.append(f"{name:<{width}} {coverage:>6} {rates}")

    for document in report["documents"]:
        row(
            str(document["document"]),
            "1/1" if document["status"] == "ok" else "0/1",
            document.get("metrics"),
        )
        if document["status"] == "error":
            lines.append(f"  ERROR: {document['error']}")
    for language, values in report["by_language"].items():
        row(
            f"language: {language}",
            f"{values['documents_scored']}/{values['documents_total']}",
            values["metrics"],
        )
    row(
        "ALL (micro-average)",
        f"{report['documents_scored']}/{report['documents_total']}",
        report["metrics"],
    )
    if not report["complete"]:
        lines.append("INCOMPLETE: aggregate metrics cover successful documents only.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--documents", type=Path, required=True)
    cli.add_argument("--ground-truth", type=Path, required=True)
    cli.add_argument("--org-config", type=Path, required=True)
    cli.add_argument("--manifest", type=Path)
    cli.add_argument("--output", type=Path)
    cli.add_argument("--overwrite", action="store_true")
    cli.add_argument(
        "--body-only", action="store_true", help="Exclude header_footer blocks"
    )
    args = cli.parse_args(argv)
    now = datetime.now(UTC)
    output = args.output or ROOT / "eval/results" / f"ocr_{now.date().isoformat()}.json"
    try:
        if output.exists() and not args.overwrite:
            raise ValueError(
                f"Result already exists: {output}; use --output or --overwrite"
            )
        import yaml
        from app.parsing.factory import parser_from_config
        from app.parsing.formats import parse_non_email
        from intellidocs_intelligence.models import OrgConfig

        config = OrgConfig.model_validate(
            yaml.safe_load(args.org_config.read_text(encoding="utf-8"))
        )
        parser = parser_from_config(config)
        samples = discover(
            args.documents.resolve(), args.ground_truth.resolve(), args.manifest
        )
        report = evaluate(
            samples,
            lambda path: parse_non_email(
                path, config.languages, parser, 20 * 1024 * 1024
            ),
            config.languages,
            include_furniture=not args.body_only,
        )
        report.update(
            created_at=now.isoformat(),
            python_version=platform.python_version(),
            org_config={
                "path": str(args.org_config),
                "sha256": digest(args.org_config),
                "ocr_engine": config.ocr_engine,
                "languages": config.languages,
            },
            normalization="NFC; whitespace collapsed; case and punctuation preserved",
            scoring="Levenshtein edits / max(1, reference length); micro-average across references",
            text_scope="body"
            if args.body_only
            else "all blocks including header/footer",
            character_unit="Unicode code point (not grapheme cluster)",
            word_unit="Unicode whitespace-delimited token",
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if args.overwrite else "x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        print(summary(report))
        print(f"Results: {output}")
        return 0 if report["complete"] else 1
    except (ValueError, OSError, ImportError, RuntimeError, KeyError) as error:
        print(f"OCR evaluation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
