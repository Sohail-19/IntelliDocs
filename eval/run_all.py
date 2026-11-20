"""Run all IntelliDocs gold-set evaluations and write a dated report."""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.retrieval.evaluate_retrieval import set_seeds
from eval.suite_config import DEFAULT_CONFIG, ROOT, SuiteConfig, fingerprint, runtime_environment
from eval.suite_documents import (
    evaluate_extraction,
    evaluate_ocr,
    evaluate_relations,
    evaluate_summary,
)
from eval.suite_report import Report, write_report
from eval.suite_search import VARIANTS, evaluate_answers, evaluate_retrieval

STAGES = (
    evaluate_ocr,
    evaluate_retrieval,
    evaluate_answers,
    evaluate_extraction,
    evaluate_relations,
    evaluate_summary,
)


def run(config_path: Path, output: Path, *, overwrite: bool = False) -> None:
    if not overwrite and any(
        (output / name).exists()
        for name in (
            "report.json",
            "report.md",
            "metrics.csv",
            "ablations.csv",
            "latency.csv",
        )
    ):
        raise FileExistsError(f"{output} contains a report. Use --overwrite or --output-dir.")
    raw = config_path.read_bytes()
    config = SuiteConfig.model_validate_json(raw)
    now = datetime.now(UTC)
    metadata = {
        "created_at": now.isoformat(),
        "seed": config.seed,
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
        "config": json.loads(raw),
        "config_file": fingerprint(config_path),
        "settings": config.model_dump(mode="json"),
        "variants": {name: vars(VARIANTS[name]) for name in ("full", *config.ablations)},
    }
    report = Report()
    with runtime_environment(config):
        set_seeds(
            config.seed,
            config.embedding.backend != "fake" or config.reranker.backend == "cross_encoder",
        )
        for stage in STAGES:
            stage(config, report)
    write_report(output, report, metadata)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "eval/results" / datetime.now(UTC).date().isoformat(),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        run(args.config, args.output_dir, overwrite=args.overwrite)
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.CalledProcessError) as error:
        print(f"Evaluation failed: {error}", file=sys.stderr)
        return 1
    print(f"Evaluation report written to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
