"""Render the same evaluation tables to JSON, Markdown, and CSV."""

import csv
import json
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter

MIN_SAMPLE = 30
Number = int | float


@dataclass(frozen=True)
class Metric:
    stage: str
    run: str
    group: str
    metric: str
    sample_size: int
    sample_unit: str
    unit: str
    status: str
    value: Number | None
    observed_value: Number | None


@dataclass
class Report:
    rows: list[Metric] = field(default_factory=list)
    latencies: dict[tuple[str, str, str, str], list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    stages: dict[str, dict[str, object]] = field(default_factory=dict)

    def add(
        self,
        stage: str,
        run: str,
        group: str,
        name: str,
        value: Number | None,
        count: int,
        sample_unit: str,
        unit: str = "ratio",
    ) -> None:
        self.rows.append(metric(stage, run, group, name, value, count, sample_unit, unit))

    def rates(
        self,
        stage: str,
        run: str,
        group: str,
        scores: Mapping[str, Number | None],
        metrics: Mapping[str, tuple[str, str]],
    ) -> None:
        for name, (denominator, sample_unit) in metrics.items():
            self.add(
                stage, run, group, name, scores[name], int(scores[denominator] or 0), sample_unit
            )

    @contextmanager
    def time(self, stage: str, run: str, operation: str, sample_unit: str) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            self.latencies[stage, run, operation, sample_unit].append(
                (perf_counter() - start) * 1000
            )

    def tables(self) -> dict[str, list[Metric]]:
        latency = []
        for (stage, run, operation, sample_unit), durations in self.latencies.items():
            for percentile in (50, 95):
                latency.append(
                    metric(
                        stage,
                        run,
                        operation,
                        f"p{percentile}",
                        quantile(durations, percentile),
                        len(durations),
                        sample_unit,
                        "ms",
                    )
                )
        baseline_runs = {"full", "before_verification", "after_verification"}
        return {
            "metrics": [row for row in self.rows if row.run in baseline_runs],
            "ablations": [row for row in self.rows if row.run not in baseline_runs],
            "latency": latency,
        }


def quantile(values: list[float], percentile: int) -> float:
    samples = sorted(values)
    index = (len(samples) - 1) * percentile / 100
    lower = int(index)
    return samples[lower] + (samples[min(lower + 1, len(samples) - 1)] - samples[lower]) * (
        index - lower
    )


def metric(
    stage: str,
    run: str,
    group: str,
    name: str,
    value: Number | None,
    count: int,
    sample_unit: str,
    unit: str,
) -> Metric:
    status = "low sample" if count < MIN_SAMPLE else ("undefined" if value is None else "ok")
    return Metric(
        stage,
        run,
        group,
        name,
        count,
        sample_unit,
        unit,
        status,
        value if status == "ok" else None,
        value,
    )


HEADERS = [
    "stage",
    "run",
    "group",
    "metric",
    "sample_size",
    "sample_unit",
    "unit",
    "value",
    "status",
]


def _display(row: Metric) -> dict[str, str | int]:
    data = asdict(row)
    data["value"] = row.status if row.value is None else f"{row.value:.4f}"
    return {name: data[name] for name in HEADERS}


def write_report(output: Path, report: Report, metadata: dict[str, object]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    tables = report.tables()
    payload = {
        **metadata,
        "minimum_sample": MIN_SAMPLE,
        "stages": report.stages,
        "tables": {name: [asdict(row) for row in rows] for name, rows in tables.items()},
    }
    (output / "report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    markdown = [
        "# Evaluation report",
        "",
        f"Git commit `{metadata['git_commit']}`  ",
        f"Working tree dirty `{metadata['git_dirty']}`  ",
        f"Seed `{metadata['seed']}`",
        "",
        f"Results with fewer than {MIN_SAMPLE} items are marked **low sample**. "
        "Percentile latency uses linear interpolation. See `report.json` for input hashes, "
        "the full configuration, and observed values for audit.",
        "",
        "## Evaluation stages",
        "",
        *(f"- **{name}** — {details['mode']}" for name, details in report.stages.items()),
        "",
    ]
    for name, rows in tables.items():
        display = [_display(row) for row in rows]
        with (output / f"{name}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADERS)
            writer.writeheader()
            writer.writerows(display)
        markdown.extend([f"## {name.title()}", ""])
        markdown.extend(
            [
                "| " + " | ".join(key.replace("_", " ").title() for key in HEADERS) + " |",
                "| " + " | ".join("---" for _ in HEADERS) + " |",
            ]
        )
        for row in display:
            markdown.append(
                "| " + " | ".join(str(value).replace("|", r"\|") for value in row.values()) + " |"
            )
        markdown.append("")
    (output / "report.md").write_text("\n".join(markdown), encoding="utf-8")
