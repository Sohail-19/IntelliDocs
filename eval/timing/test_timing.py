import csv
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from docx import Document

from e2e.mock_llm import answer
from eval.timing import run
from eval.timing.core import Core, Exchange
from scripts.seed_demo import PILOT, docx

SHEET = run.HERE / "timing_sheet.csv"


def measurements(tmp_path: Path) -> Path:
    system = tmp_path / "system.json"
    run.write(
        system,
        {
            "measured_at": "fixture",
            "settings": {},
            "gold_sha256": run.digest(run.QUESTION_SET),
            "rows": [
                {
                    "question_id": row["id"],
                    "system_seconds": index + 1,
                    "system_status": "timeout" if index == 0 else "answered",
                }
                for index, row in enumerate(run.questions(run.QUESTION_SET))
            ],
        },
    )
    return system


def test_blank_sheet_reports_machine_times_without_claiming_speedup(tmp_path: Path) -> None:
    result = run.compare(run.QUESTION_SET, SHEET, measurements(tmp_path))
    assert result["all_attempts"]["system"] == {
        "count": 12,
        "median_seconds": 6.5,
        "p95_seconds": pytest.approx(11.45),
    }
    assert result["manual_outcomes"] == {"unmeasured": 12}
    assert result["system_outcomes"] == {"timeout": 1, "unreviewed": 11}
    assert result["manual_to_system_median_ratio"] is None


def test_partial_trials_and_failures_are_not_counted_as_speedups(tmp_path: Path) -> None:
    with SHEET.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    system = measurements(tmp_path)
    sheet = tmp_path / "entered.csv"

    def save() -> None:
        with sheet.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    for index, row in enumerate(rows):
        row.update(manual_seconds=str(10 * (index + 1)), manual_outcome="correct")
        row.update(system_outcome="incorrect" if index == 1 else "correct" if index else "")
    rows[-1]["manual_seconds"] = rows[-1]["manual_outcome"] = ""
    save()
    assert run.compare(run.QUESTION_SET, sheet, system)["manual_to_system_median_ratio"] is None
    rows[-1].update(manual_seconds="120", manual_outcome="correct")
    save()
    report = run.compare(run.QUESTION_SET, sheet, system)
    assert report["paired_correct"]["system"]["count"] == 10
    assert report["manual_to_system_median_ratio"] == 10
    assert report["manual_to_system_p95_ratio"] == pytest.approx(10)
    rows[0]["system_outcome"] = "correct"
    save()
    with pytest.raises(ValueError, match="only for a returned answer"):
        run.compare(run.QUESTION_SET, sheet, system)


def test_measure_authenticates_once_per_org_and_keeps_http_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logins, requests = [], []

    def login(_core: Core, org: str, _email: str, _password: str) -> str:
        logins.append(org)
        return f"secret-{org}"

    def request(_core: Core, url: str, *, token: str) -> Exchange:
        requests.append((url, token))
        return (
            Exchange(0.1, 502, b"", "http_error")
            if len(requests) == 1
            else Exchange(
                0.25,
                200,
                b'{"abstained": false, "answer_text": "Text", "claims": []}',
            )
        )

    monkeypatch.setattr(Core, "login", login)
    monkeypatch.setattr(Core, "request", request)
    access = tmp_path / "access.json"
    run.write(
        access,
        {
            "core_url": "http://127.0.0.1:53000",
            "organizations": {
                org: {"operator": {"email": "pilot@example.test", "password": "private-password"}}
                for org in ("example", "example2")
            },
        },
    )
    report = run.measure(run.QUESTION_SET, access, 30, 7)
    assert logins == ["example", "example2"] and len(requests) == 12
    assert [parse_qs(urlsplit(url).query)["language"][0] for url, _ in requests] == ["en"] * 6 + [
        "es"
    ] * 6
    assert report["rows"][0]["system_status"] == "http_error"
    assert report["rows"][1]["system_seconds"] == 0.25
    assert "secret-" not in json.dumps(report) and "private-password" not in json.dumps(report)


def test_pilot_documents_round_trip_and_support_the_gold(tmp_path: Path) -> None:
    pilot = run.read_object(PILOT)
    for question in run.questions(run.QUESTION_SET):
        rows = pilot[question["org_id"]]
        document = next(row for row in rows if row["filename"] == question["source"])
        path = tmp_path / question["source"]
        docx(path, document["paragraphs"])
        assert [paragraph.text for paragraph in Document(path).paragraphs] == document["paragraphs"]
        result = answer(
            {
                "query": question["query"],
                "chunks": [
                    {"chunk_id": row["filename"], "text": "\n\n".join(row["paragraphs"])}
                    for row in rows
                ],
            }
        )
        [claim] = result["claims"]
        assert claim["chunk_id"] == question["source"]
        assert question["answer"].casefold() in claim["quote"].casefold()
