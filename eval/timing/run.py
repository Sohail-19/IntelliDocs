"""Measure Core answer requests and compare them with human stopwatch readings."""

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, TypedDict
from urllib.parse import urlencode

from .core import Core

HERE = Path(__file__).resolve().parent
QUESTION_SET = HERE / "questions.jsonl"
OUTCOMES = {"correct", "incorrect", "timeout"}
SYSTEM_STATUSES = {
    "answered",
    "abstained",
    "invalid_response",
    "http_error",
    "timeout",
    "connection_error",
}


class Question(TypedDict):
    id: str
    org_id: str
    language: str
    query: str
    answer: str
    source: str
    page: int


def questions(path: Path) -> list[Question]:
    rows: list[Question] = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not 10 <= len(rows) <= 15 or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Use 10 to 15 gold questions with unique IDs")
    return rows


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def measure(gold_path: Path, credentials: Path, timeout: float, top_k: int) -> dict[str, Any]:
    gold = questions(gold_path)
    access = read_object(credentials)
    core = Core(access["core_url"], timeout)
    stack = access.get("stack", {"mode": "unspecified"})
    login = access["organizations"]
    tokens = {}
    for org in dict.fromkeys(row["org_id"] for row in gold):
        user = login[org]["operator"]
        tokens[org] = core.login(org, user["email"], user["password"])
    rows = []
    for question in gold:
        query = urlencode(
            {"query": question["query"], "language": question["language"], "top_k": top_k}
        )
        response = core.request(f"/api/answer?{query}", token=tokens[question["org_id"]])
        row = {
            "question_id": question["id"],
            "system_seconds": response.seconds,
            "http_status": response.status,
            "system_status": response.error or "invalid_response",
        }
        if response.status == 200:
            try:
                answer = response.json()
            except (ValueError, UnicodeError):
                answer = {}
            if (
                type(answer.get("abstained")) is bool
                and isinstance(answer.get("answer_text"), str)
                and isinstance(answer.get("claims"), list)
            ):
                row.update(
                    system_status="abstained" if answer["abstained"] else "answered",
                    answer_text=answer["answer_text"],
                    claims=answer["claims"],
                    abstain_reason=answer.get("abstain_reason"),
                    answer_model=answer.get("model"),
                )
        rows.append(row)
    return {
        "measured_at": datetime.now(UTC).isoformat(),
        "gold_sha256": digest(gold_path),
        "settings": {
            "core_url": core.url,
            "top_k": top_k,
            "socket_timeout_seconds": timeout,
            "stack": {
                name: stack[name]
                for name in (
                    "mode",
                    "provider",
                    "model",
                    "embedder",
                    "reranker",
                )
                if name in stack
            },
        },
        "rows": rows,
    }


def elapsed(value: str | float, label: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{label} must be a measured number of seconds greater than zero")
    return seconds


def stats(samples: list[float]) -> dict[str, int | float | None]:
    if not samples:
        return {"count": 0, "median_seconds": None, "p95_seconds": None}
    ordered = sorted(samples)
    index = 0.95 * (len(ordered) - 1)
    lo, hi = math.floor(index), math.ceil(index)
    return {
        "count": len(samples),
        "median_seconds": median(samples),
        "p95_seconds": ordered[lo] + (index - lo) * (ordered[hi] - ordered[lo]),
    }


def compare(gold_path: Path, timing_sheet: Path, system_path: Path) -> dict[str, Any]:
    gold = {row["id"]: row for row in questions(gold_path)}
    machine = read_object(system_path)
    if machine["gold_sha256"] != digest(gold_path):
        raise ValueError("System measurements used a different gold set")
    responses = {row["question_id"]: row for row in machine["rows"]}
    with timing_sheet.open(encoding="utf-8-sig", newline="") as stream:
        entries = list(csv.DictReader(stream))
    human = {row["question_id"]: row for row in entries}
    if (
        len(entries) != len(gold)
        or len(machine["rows"]) != len(gold)
        or human.keys() != gold.keys()
        or responses.keys() != gold.keys()
    ):
        raise ValueError(
            "Each gold question needs exactly one sheet row and one system measurement"
        )

    manual_all, system_all, manual_paired, system_paired = [], [], [], []
    manual_outcomes: Counter[str] = Counter()
    system_outcomes: Counter[str] = Counter()
    rows = []
    for key, question in gold.items():
        row, system = human[key], responses[key]
        if (row["org_id"], row["language"], row["query"]) != (
            question["org_id"],
            question["language"],
            question["query"],
        ):
            raise ValueError(f"Timing sheet changed the gold question {key}")
        duration, outcome, review = (
            row[field].strip()
            for field in (
                "manual_seconds",
                "manual_outcome",
                "system_outcome",
            )
        )
        if bool(duration) != bool(outcome) or (outcome and outcome not in OUTCOMES):
            raise ValueError(
                f"Record both a measured duration and a valid manual outcome for {key}"
            )
        status = system["system_status"]
        if (
            status not in SYSTEM_STATUSES
            or review not in {"", "correct", "incorrect"}
            or (review and status != "answered")
        ):
            raise ValueError(
                f"Review must be correct or incorrect only for a returned answer ({key})"
            )
        manual = elapsed(duration, key) if duration else None
        measured = elapsed(system["system_seconds"], key)
        if manual is not None:
            manual_all.append(manual)
        system_all.append(measured)
        manual_outcomes[outcome or "unmeasured"] += 1
        system_outcomes[review or ("unreviewed" if status == "answered" else status)] += 1
        if manual is not None and outcome == review == "correct":
            manual_paired.append(manual)
            system_paired.append(measured)
        rows.append(
            {
                "question_id": key,
                "manual_seconds": manual,
                "manual_outcome": outcome or None,
                "system_seconds": measured,
                "system_status": status,
                "system_outcome": review or None,
            }
        )

    complete = len(manual_all) == len(gold) and not system_outcomes["unreviewed"]
    baseline, automated = stats(manual_paired), stats(system_paired)
    comparison = "complete" if complete else "pending manual measurements or review"
    if complete and not manual_paired:
        comparison = "no questions answered correctly by both methods"

    def ratio(metric: str) -> float | None:
        manual, system = baseline[metric], automated[metric]
        return manual / system if complete and manual is not None and system else None

    return {
        "system_measured_at": machine["measured_at"],
        "settings": machine["settings"],
        "inputs": {
            "gold_sha256": digest(gold_path),
            "timing_sheet_sha256": digest(timing_sheet),
            "system_sha256": digest(system_path),
        },
        "questions": len(gold),
        "comparison_status": comparison,
        "interpretation": "Small pilot. Fixture-mode timings describe a scripted stack only.",
        "manual_outcomes": dict(manual_outcomes),
        "system_outcomes": dict(system_outcomes),
        "all_attempts": {"manual": stats(manual_all), "system": stats(system_all)},
        "paired_correct": {"manual": baseline, "system": automated},
        "manual_to_system_median_ratio": ratio("median_seconds"),
        "manual_to_system_p95_ratio": ratio("p95_seconds"),
        "rows": rows,
    }


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")


def positive_seconds(value: str) -> float:
    try:
        return elapsed(value, "Timeout")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    record = commands.add_parser("measure", help="Measure each question through Core once")
    record.add_argument("--credentials", type=Path, required=True)
    record.add_argument("--timeout", type=positive_seconds, default=90.0)
    record.add_argument("--top-k", type=int, choices=range(1, 101), default=10)
    report = commands.add_parser("report", help="Compare system timings with the completed sheet")
    report.add_argument("--sheet", type=Path, required=True)
    report.add_argument("--system", type=Path, required=True)
    for command in (record, report):
        command.add_argument("--gold", type=Path, default=QUESTION_SET)
        command.add_argument("--output", type=Path, required=True)
    args = cli.parse_args(argv)
    os.umask(0o077)
    try:
        if args.output.exists():
            raise FileExistsError(f"Output already exists: {args.output}")
        if args.command == "measure":
            result = measure(args.gold, args.credentials, args.timeout, args.top_k)
            print(f"Recorded {len(result['rows'])} answer attempts")
        else:
            result = compare(args.gold, args.sheet, args.system)
            for group in ("all_attempts", "paired_correct"):
                for mode, summary in result[group].items():
                    print(
                        f"{group} {mode} — n={summary['count']}, "
                        f"median={summary['median_seconds']} s, p95={summary['p95_seconds']} s"
                    )
            ratio = result["manual_to_system_median_ratio"]
            if ratio is not None:
                print(f"Manual/system median ratio {ratio:.2f}× (small pilot)")
                p95_ratio = result["manual_to_system_p95_ratio"]
                print(f"Manual/system p95 ratio {p95_ratio:.2f}× (small pilot)")
            else:
                print(f"No speedup ratio available ({result['comparison_status']})")
        write(args.output, result)
        print(f"Wrote {args.output}")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Timing trial failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
