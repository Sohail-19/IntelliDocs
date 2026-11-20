"""Train a TF-IDF document classifier and score it on a separate parsed test set."""

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from app.classification import classification_text
from app.classification.tfidf import TfidfClassifier, train_tfidf
from intellidocs_intelligence.models import ClassificationConfig, OrgConfig, ParseResult

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Example:
    doc_id: str
    digest: str
    text: str
    label: str
    language: str


def load_split(directory: Path, config: OrgConfig) -> list[Example]:
    examples = []
    for path in sorted(directory.rglob("*.json")):
        try:
            document = ParseResult.model_validate_json(path.read_bytes()).document
            label = document.doc_type
            if label is None or label not in config.document_types:
                raise ValueError(f"document.doc_type {label!r} is not a configured label")
            text = classification_text(document)
            if document.status != "ready" or not text:
                raise ValueError("expected a ready document with parsed text")
            counts = Counter(page.language for page in document.pages)
            language = document.language or (counts.most_common(1)[0][0] if counts else None)
            if language is None or language not in config.languages:
                raise ValueError(f"document language {language!r} is not configured")
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
        examples.append(
            Example(document.id, hashlib.sha256(text.encode()).hexdigest(), text, label, language)
        )
    if not examples:
        raise ValueError(f"{directory} must contain labelled ParseResult JSON files")
    return examples


Scored = tuple[Example, str | None]


def type_scores(rows: Sequence[Scored], label: str) -> dict[str, int | float | None]:
    support = sum(example.label == label for example, _ in rows)
    true_positive = sum(example.label == prediction == label for example, prediction in rows)
    predicted = sum(prediction == label for _, prediction in rows)
    return {
        "support": support,
        "accuracy": true_positive / support if support else None,
        "f1": 2 * true_positive / (support + predicted) if support + predicted else None,
    }


def aggregate(rows: Sequence[Scored], labels: Sequence[str]) -> dict[str, int | float | None]:
    f1 = [score for label in labels if (score := type_scores(rows, label)["f1"]) is not None]
    return {
        "support": len(rows),
        "accuracy": sum(example.label == prediction for example, prediction in rows) / len(rows)
        if rows
        else None,
        "macro_f1": sum(f1) / len(f1) if f1 else None,
        "abstentions": sum(prediction is None for _, prediction in rows),
    }


def train_and_evaluate(folder: Path, config: OrgConfig, artifact: Path) -> dict[str, object]:
    training, testing = (load_split(folder / split, config) for split in ("train", "test"))
    if {row.doc_id for row in training} & {row.doc_id for row in testing} or {
        row.digest for row in training
    } & {row.digest for row in testing}:
        raise ValueError("Training and test splits contain the same document ID or parsed text")
    labels = config.document_types
    model = train_tfidf([(row.text, row.label) for row in training], labels)
    classifier = TfidfClassifier(model, labels)
    minimum = (config.classification or ClassificationConfig(backend="disabled")).min_confidence
    scored: list[Scored] = []
    for row in testing:
        prediction = classifier.predict(row.text)
        scored.append(
            (
                row,
                prediction.document_type
                if prediction and prediction.confidence >= minimum
                else None,
            )
        )
    model.save(artifact)
    return {
        "model_version": model.version,
        "train_documents": len(training),
        "min_confidence": minimum,
        "overall": aggregate(scored, labels),
        "by_document_type": {label: type_scores(scored, label) for label in labels},
        "by_language": {
            language: aggregate([row for row in scored if row[0].language == language], labels)
            for language in config.languages
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder containing train/ and test/ ParseResults")
    parser.add_argument("--org-config", type=Path, required=True)
    parser.add_argument("--model", type=Path, help="Defaults to models/classification/<org>.json")
    parser.add_argument(
        "--report", type=Path, help="Defaults to eval/results/classification_<org>.json"
    )
    args = parser.parse_args()
    name = args.org_config.stem
    artifact = args.model or ROOT / "models" / "classification" / f"{name}.json"
    report = args.report or ROOT / "eval" / "results" / f"classification_{name}.json"
    if artifact.resolve() == report.resolve():
        parser.error("Choose different paths for the model and metrics report")
    # Protect the held-out input and the org file from accidental overwrites.
    for output in (artifact, report):
        if (
            args.folder.resolve() in output.resolve().parents
            or output.resolve() == args.org_config.resolve()
        ):
            parser.error("Write results outside the input folder and org config")
    try:
        config = OrgConfig.model_validate(
            yaml.safe_load(args.org_config.read_text(encoding="utf-8"))
        )
        metrics = train_and_evaluate(args.folder, config, artifact)
        report.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(metrics, ensure_ascii=False, indent=2)
        report.write_text(serialized + "\n", encoding="utf-8")
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.exit(1, f"{error}\n")
    print(serialized)
    print(f"Model saved to {artifact}\nMetrics saved to {report}")


if __name__ == "__main__":
    main()
