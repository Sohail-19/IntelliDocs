"""Exercise the complete evaluation harness against the bundled synthetic gold sets."""

import csv
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from intellidocs_intelligence.models import Page, ParsedDocument, ParseResult, TextBlock

from eval import suite_documents
from eval.run_all import main
from eval.suite_config import DEFAULT_CONFIG, ROOT
from eval.suite_report import metric, quantile


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Bundled evaluation must run offline")

    def parse(source: Path, *_args: object) -> ParseResult:
        text = {
            "scanned.pdf": "Original scanned fixture text.",
            "upload.png": "Original upload fixture",
        }[source.name]
        block = TextBlock(type="paragraph", text=text, bbox=[0, 0, 1, 1], reading_order=0)
        return ParseResult(
            document=ParsedDocument(
                id=source.stem,
                filename=source.name,
                mime_type="image/png",
                source_uri=str(source),
                status="ready",
                pages=[Page(page=1, language="en", blocks=[block])],
            ),
            parser_name="test",
            parser_version="1",
            warnings=[],
        )

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(suite_documents, "parse_non_email", parse)


def row(
    rows: list[dict[str, Any]], stage: str, name: str, run: str = "full", group: str = "overall"
) -> dict[str, Any]:
    return next(
        item
        for item in rows
        if (item["stage"], item["run"], item["metric"], item["group"]) == (stage, run, name, group)
    )


def test_bundled_gold_traverses_every_stage(tmp_path: Path) -> None:
    assert main(["--output-dir", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "report.json").read_bytes())
    assert list(report["stages"]) == [
        "ocr",
        "retrieval",
        "answer",
        "extraction",
        "relations",
        "summary",
    ]
    assert (
        report["git_commit"]
        == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )
    assert report["seed"] == 42 and report["config"] == json.loads(DEFAULT_CONFIG.read_bytes())
    metrics, ablations = report["tables"]["metrics"], report["tables"]["ablations"]
    assert row(metrics, "ocr", "cer")["observed_value"] == 0
    assert (
        row(metrics, "retrieval", "recall_at_5", group="query_type=cross_lingual")["sample_size"]
        > 0
    )
    assert row(metrics, "retrieval", "ndcg_at_10", group="language=hi")["sample_size"] > 0
    before = row(metrics, "answer", "unsupported_claim_rate", "before_verification")
    after = row(metrics, "answer", "unsupported_claim_rate", "after_verification")
    assert before["observed_value"] == 0.5 and after["observed_value"] == 0
    assert (
        row(metrics, "answer", "abstention_rate_unanswerable", "after_verification")[
            "observed_value"
        ]
        == 1
    )
    assert row(metrics, "extraction", "citation_faithfulness")["observed_value"] == pytest.approx(
        2 / 3
    )
    assert row(metrics, "relations", "false_supersession_rate")["observed_value"] == 0.25
    assert row(metrics, "summary", "point_coverage")["observed_value"] == 1
    assert {item["run"] for item in ablations} == set(report["config"]["ablations"])
    assert (
        row(ablations, "answer", "unsupported_claim_rate", "verifier_off")["observed_value"]
        == before["observed_value"]
    )
    assert {r["stage"] for r in report["tables"]["latency"]} == set(report["stages"])
    assert {r["metric"] for r in report["tables"]["latency"]} == {"p50", "p95"}
    for name, values in report["tables"].items():
        with (tmp_path / f"{name}.csv").open(newline="", encoding="utf-8") as handle:
            csv_rows = list(csv.DictReader(handle))
        assert len(csv_rows) == len(values)
        assert all(entry["value"] == "low sample" for entry in csv_rows)
        assert all(entry["value"] is None and entry["status"] == "low sample" for entry in values)
    assert "**low sample**" in (tmp_path / "report.md").read_text()
    assert main(["--output-dir", str(tmp_path)]) == 1


def test_custom_cutoffs_and_ablations_are_used(tmp_path: Path) -> None:
    settings = json.loads(DEFAULT_CONFIG.read_bytes())
    settings["retrieval"].update(recall_at=[1, 3], ndcg_at=3)
    settings["ablations"] = ["verifier_off"]
    config = tmp_path / "config.json"
    config.write_text(json.dumps(settings))
    assert main(["--output-dir", str(tmp_path), "--config", str(config)]) == 0
    tables = json.loads((tmp_path / "report.json").read_bytes())["tables"]
    assert {item["run"] for item in tables["ablations"]} == {"verifier_off"}
    assert {item["metric"] for item in tables["metrics"] if item["stage"] == "retrieval"} == {
        "recall_at_1",
        "recall_at_3",
        "mrr",
        "ndcg_at_3",
    }


def test_small_sample_boundary_and_percentiles() -> None:
    assert metric("ocr", "full", "overall", "wer", 0.15, 29, "documents", "ratio").value is None
    assert metric("ocr", "full", "overall", "wer", 0.15, 30, "documents", "ratio").value == 0.15
    assert quantile([0, 20, 10], 50) == 10
    assert quantile([0, 20], 95) == 19
