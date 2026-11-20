"""Prepare local CUAD and SROIE gold sets and reproducible extraction inputs."""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import yaml
from app.extraction.extractor import DraftObligation
from app.extraction.processor import ObligationProcessor
from intellidocs_intelligence.models import Chunk, OrgConfig

from .adapters import Corpus, adapt_cuad, adapt_sroie, write_jsonl

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data/public"
DEFAULT_ORG = ROOT / "config/orgs/example.yaml"
CUAD_URL = "https://raw.githubusercontent.com/TheAtticusProject/cuad/master/data.zip"
SROIE_API = "https://datasets-server.huggingface.co/rows?dataset=jsdnrs/ICDAR2019-SROIE"
SROIE_URL = SROIE_API + "&config=default&split=test&offset=0&length=3"
BASELINE = "configured-cue-sentences.v1"


def file_info(path: Path) -> dict[str, str]:
    return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_corpus(corpus: Corpus, directory: Path, org: OrgConfig) -> ObligationProcessor:
    directory.mkdir(parents=True, exist_ok=True)
    write_jsonl(directory / "sources.jsonl", corpus.sources)
    write_jsonl(directory / "chunks.jsonl", corpus.chunks)
    write_jsonl(directory / "retrieval_gold.jsonl", corpus.retrieval_gold)
    write_jsonl(directory / "extraction_gold.jsonl", corpus.extraction_gold)

    processor = ObligationProcessor(org)
    by_doc: dict[str, list[Chunk]] = defaultdict(list)
    for chunk in corpus.chunks:
        by_doc[chunk.doc_id].append(chunk)
    # The baseline uses only source text and configured cues. It never reads gold answers.
    outputs = []
    for source in corpus.sources:
        document = source.document
        chunks = by_doc[document.id]
        drafts = [
            DraftObligation(
                chunk_id=candidate.chunk.id,
                action=sentence,
                quote=sentence,
                condition=None,
                obligation_type="duty",
                owner_text="",
                deadline_text="",
            )
            for candidate in processor.detector.detect(chunks)
            for sentence in candidate.sentences
            if processor.detector.has_cue(sentence, candidate.chunk.language)
        ]
        outputs.append(processor.process(document, chunks, drafts))
    write_jsonl(directory / "predictions.jsonl", outputs)
    return processor


def prepare(raw: Path, output: Path, org_path: Path, count: int) -> None:
    org = OrgConfig.model_validate(yaml.safe_load(org_path.read_text(encoding="utf-8")))
    cuad = raw / "cuad-data.zip"
    sroie = raw / "sroie-test.json"
    receipt_dir = output / "sroie"
    selection = f"first {count} supplied test records, in published order"
    inputs = [
        ("cuad", adapt_cuad(cuad, count), CUAD_URL, [file_info(cuad)]),
    ]
    receipts = adapt_sroie(sroie, raw, receipt_dir, count)
    inputs.append(
        (
            "sroie",
            receipts,
            SROIE_URL,
            [file_info(sroie), *(file_info(raw / member["image"]) for member in receipts.members)],
        )
    )
    for name, corpus, url, files in inputs:
        directory = output / name
        processor = save_corpus(corpus, directory, org)
        write_json(
            directory / "provenance.json",
            {
                "dataset": name,
                "source": url,
                "selection": selection,
                "source_files": files,
                "members": corpus.members,
                "retrieval_queries": len(corpus.retrieval_gold),
                "extraction_baseline": {
                    "model": BASELINE,
                    "llm": None,
                    "org_config": {"path": str(org_path), **file_info(org_path)},
                    "language_rules": {
                        name: rules.model_dump(mode="json")
                        for name, rules in org.obligation_detection.items()
                    },
                    "fuzzy_threshold": processor.verifier.config.fuzzy_threshold,
                    "confidence_weights": processor.weights.model_dump(mode="json"),
                },
            },
        )
    # The English-only evaluation config avoids requiring unrelated OCR language packs.
    ocr_org = OrgConfig(
        languages=["en"],
        ocr_engine="tesseract",
        document_structure=org.document_structure,
        document_types=[],
        departments=[],
        routing_rules=[],
    )
    write_json(receipt_dir / "ocr_org.json", ocr_org.model_dump(mode="json", exclude_none=True))
    write_json(
        receipt_dir / "ocr_manifest.json",
        {
            "documents": [
                {
                    "file": f"en/{member['image']}",
                    "references": [
                        {"language": "en", "pages": [1], "text": f"en/{member['image']}.txt"},
                    ],
                }
                for member in receipts.members
            ]
        },
    )


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--raw", type=Path, default=DATA / "raw")
    cli.add_argument("--output", type=Path, default=DATA / "prepared")
    cli.add_argument("--org-config", type=Path, default=DEFAULT_ORG)
    cli.add_argument("--count", type=int, default=3)
    args = cli.parse_args()
    if args.count < 1:
        cli.error("--count must be positive")
    prepare(args.raw, args.output, args.org_config, args.count)


if __name__ == "__main__":
    main()
