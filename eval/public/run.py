"""Run the existing evaluators on prepared public datasets with local models."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .prepare import BASELINE, DATA, ROOT, write_json

E5_MODEL = "intfloat/multilingual-e5-small"


def run(prepared: Path, model_path: Path | None, seed: int, overwrite: bool) -> None:
    if model_path is not None and not model_path.is_dir():
        raise ValueError("--e5-model must be a local multilingual E5-small model directory")
    env = {
        **os.environ,
        "PYTHONHASHSEED": str(seed),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "EMBEDDING_BACKEND": "sentence_transformers" if model_path else "fake",
        "EMBEDDING_MODEL": str(model_path.resolve()) if model_path else "",
        "EMBEDDING_DEVICE": "cpu",
        "EMBEDDING_DOCUMENT_PROMPT": "passage: " if model_path else "",
        "EMBEDDING_QUERY_PROMPT": "query: " if model_path else "",
        "SPARSE_EMBEDDING_BACKEND": "bm25",
        "RERANKER_BACKEND": "fake",
        "RERANKER_MODEL": "lexical-term-overlap",
        "RERANKER_DEVICE": "cpu",
        "OMP_NUM_THREADS": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    now = datetime.now(UTC).date().isoformat()

    def evaluate(
        dataset: str, kind: str, script: str, args: list[str], models: dict[str, object]
    ) -> None:
        output = ROOT / "eval/results" / f"public_{dataset}_{kind}_{now}.json"
        provenance = json.loads(
            (prepared / dataset / "provenance.json").read_text(encoding="utf-8")
        )
        command = [sys.executable, script, *args, "--output", str(output)]
        if overwrite:
            command.append("--overwrite")
        previous_write = output.stat().st_mtime_ns if output.exists() else None
        result = subprocess.run(command, cwd=ROOT, env=env, check=False)
        # An incomplete OCR run still produces useful scored and failed-document records.
        if output.exists() and output.stat().st_mtime_ns != previous_write:
            report = json.loads(output.read_text(encoding="utf-8"))
            report["public_dataset"] = provenance
            report["public_run_config"] = {"seed": seed, "models": models}
            write_json(output, report)
        result.check_returncode()

    for dataset in ("cuad", "sroie"):
        directory = prepared / dataset
        evaluate(
            dataset,
            "retrieval",
            "eval/retrieval/evaluate_retrieval.py",
            [
                "--gold",
                str(directory / "retrieval_gold.jsonl"),
                "--chunks",
                str(directory / "chunks.jsonl"),
                "--seed",
                str(seed),
            ],
            {
                "dense": E5_MODEL if model_path else "fake-sha256-v1",
                "sparse": "BM25",
                "reranker": "lexical-term-overlap",
            },
        )
        evaluate(
            dataset,
            "extraction",
            "eval/extraction/evaluate_extraction.py",
            [
                "--gold",
                str(directory / "extraction_gold.jsonl"),
                "--sources",
                str(directory / "sources.jsonl"),
                "--predictions",
                str(directory / "predictions.jsonl"),
            ],
            {"extractor": BASELINE, "llm": None},
        )
    receipt_dir = prepared / "sroie"
    version = subprocess.check_output(["tesseract", "--version"], text=True).splitlines()[0]
    evaluate(
        "sroie",
        "ocr",
        "eval/ocr/evaluate.py",
        [
            "--documents",
            str(receipt_dir / "documents"),
            "--ground-truth",
            str(receipt_dir / "ground-truth"),
            "--manifest",
            str(receipt_dir / "ocr_manifest.json"),
            "--org-config",
            str(receipt_dir / "ocr_org.json"),
        ],
        {"engine": version, "language_pack": "eng"},
    )


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--prepared", type=Path, default=DATA / "prepared")
    cli.add_argument("--e5-model", type=Path, help="Local intfloat/multilingual-e5-small weights")
    cli.add_argument("--seed", type=int, default=42)
    cli.add_argument("--overwrite", action="store_true")
    args = cli.parse_args()
    if not 0 <= args.seed < 2**32:
        cli.error("--seed must be an unsigned 32-bit integer")
    run(args.prepared.resolve(), args.e5_model, args.seed, args.overwrite)


if __name__ == "__main__":
    main()
