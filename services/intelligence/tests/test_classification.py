"""Offline classifier and ingest regressions using original synthetic text."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_api import validate_contract

from app.classification import classifier_from_config
from app.classification.tfidf import TfidfClassifier, TfidfModel, train_tfidf
from app.classification.zero_shot import ZeroShotClassifier
from app.storage import LocalStorage
from intellidocs_intelligence.llm import FakeProvider
from intellidocs_intelligence.main import app
from intellidocs_intelligence.models import OrgConfig

ROOT = Path(__file__).resolve().parents[3]
LABELS = ["water_quality_notice", "service_interruption"]
QUALITY, INTERRUPTION = LABELS
SAMPLES = [
    ("cloro residual muestreo laboratorio", QUALITY),
    ("chlore résiduel contrôle laboratoire", QUALITY),
    ("corte suministro reparación tubería", INTERRUPTION),
    ("coupure conduite réparation distribution", INTERRUPTION),
]
TEST = [
    ("cloro muestreo laboratorio semanal", QUALITY, "es"),
    ("chlore résiduel contrôle journalier", QUALITY, "fr"),
    ("corte suministro tubería temporal", INTERRUPTION, "es"),
    ("coupure réparation distribution provisoire", INTERRUPTION, "fr"),
]


class InspectingProvider(FakeProvider):
    def generate(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        request = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        assert request["allowed_types"] == LABELS
        assert schema["properties"]["document_type"]["enum"] == LABELS
        return super().generate(prompt, schema)


def test_zero_shot_rejects_an_unconfigured_label_even_if_provider_returns_it() -> None:
    provider = InspectingProvider("test-model", {"document_type": "unconfigured", "confidence": 1})
    classifier = ZeroShotClassifier(LABELS, lambda: provider)
    assert classifier.predict("water report") is None
    provider.response = {"document_type": QUALITY, "confidence": 0}
    assert classifier.predict("unclear excerpt") is None
    provider.response = {"document_type": QUALITY, "confidence": 0.9}
    prediction = classifier.predict("cloro residual")
    assert prediction is not None and prediction.document_type == QUALITY
    assert provider.calls == 3


def test_model_rejects_unknown_training_labels_and_other_org_taxonomies() -> None:
    with pytest.raises(ValueError, match="Unknown.*unconfigured"):
        train_tfidf([*SAMPLES, ("text in a new class", "unconfigured")], LABELS)
    model = train_tfidf(SAMPLES, LABELS)
    with pytest.raises(ValueError, match="labels must match"):
        TfidfClassifier(model, [QUALITY, "unconfigured"])
    assert TfidfClassifier(model, LABELS).predict("zzzzzz xxxxyyyy") is None


def parsed_example(text: str, label: str, language: str, identifier: str) -> dict[str, object]:
    return {
        "document": {
            "id": identifier,
            "filename": identifier + ".pdf",
            "source_uri": "file:///" + identifier + ".pdf",
            "mime_type": "application/pdf",
            "doc_type": label,
            "status": "ready",
            "pages": [
                {
                    "page": 1,
                    "language": language,
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": text,
                            "bbox": [0, 0, 10, 10],
                            "reading_order": 0,
                        },
                    ],
                }
            ],
        },
        "parser_name": "fixture",
        "parser_version": "1",
        "warnings": [],
    }


def test_training_cli_saves_loadable_model_and_held_out_metrics(tmp_path: Path) -> None:
    inputs = tmp_path / "parsed"
    training = [
        (text, label, language)
        for (text, label), language in zip(SAMPLES, ("es", "fr") * 2, strict=True)
    ]
    for split, examples in (("train", training), ("test", TEST)):
        folder = inputs / split
        folder.mkdir(parents=True)
        for i, (text, label, language) in enumerate(examples):
            identifier = f"{split}-{i}"
            example = parsed_example(text, label, language, identifier)
            (folder / f"{identifier}.json").write_text(json.dumps(example), encoding="utf-8")
    model, report = tmp_path / "model.json", tmp_path / "metrics.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "eval.classification.train",
            str(inputs),
            "--org-config",
            str(ROOT / "config/orgs/example2.yaml"),
            "--model",
            str(model),
            "--report",
            str(report),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    metrics = json.loads(report.read_text(encoding="utf-8"))
    assert metrics["overall"] == {"support": 4, "accuracy": 1.0, "macro_f1": 1.0, "abstentions": 0}
    assert metrics["by_document_type"][QUALITY] == {"support": 2, "accuracy": 1.0, "f1": 1.0}
    assert metrics["by_language"]["fr"]["macro_f1"] == 1.0
    assert metrics["by_language"]["es"]["accuracy"] == 1.0
    predictor = TfidfClassifier(TfidfModel.model_validate_json(model.read_bytes()), LABELS)
    result = predictor.predict(TEST[0][0])
    assert result is not None and result.document_type == QUALITY


def test_missing_text_model_uses_only_the_configured_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = InspectingProvider("test-model", {"document_type": QUALITY, "confidence": 0.9})
    monkeypatch.setattr("app.classification.zero_shot.provider_from_env", lambda: provider)
    config = json.loads((ROOT / "config/orgs/example2.yaml").read_text(encoding="utf-8"))
    config["classification"] = {
        "backend": "tfidf",
        "model_path": str(tmp_path / "missing.json"),
        "llm_fallback": True,
    }
    classifier = classifier_from_config(OrgConfig.model_validate(config))
    assert classifier is not None and provider.calls == 0
    prediction = classifier.predict("control de cloro")
    assert prediction is not None and prediction.document_type == QUALITY and provider.calls == 1


@pytest.mark.parametrize("backend", ["tfidf", "missing"])
def test_ingest_stores_the_suggestion_separately_and_never_changes_user_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    config_dir, storage_dir = tmp_path / "orgs", tmp_path / "uploads"
    config_dir.mkdir()
    config = json.loads((ROOT / "config/orgs/example2.yaml").read_text(encoding="utf-8"))
    model = tmp_path / "classifier.json"
    if backend == "tfidf":
        train_tfidf(SAMPLES, LABELS).save(model)
    config["classification"] = {"backend": "tfidf", "model_path": str(model)}
    (config_dir / "example2.yaml").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("ORG_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("STORAGE_DIR", str(storage_dir))
    message = (
        b"From: fixture@example.test\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Control de cloro residual, muestreo laboratorio.\r\n"
    )
    response = TestClient(app).post(
        "/v1/ingest",
        data={"org_id": "example2", "doc_type": INTERRUPTION},
        files={"file": ("sample.eml", message, "message/rfc822")},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    validate_contract(payload, "ingest-response.v1.schema.json")
    stored = LocalStorage(storage_dir).load_result(payload["document_id"])
    assert (
        payload["parse_result"]["document"]["doc_type"] == stored.document.doc_type == INTERRUPTION
    )
    if backend == "tfidf":
        assert payload["parse_result"]["document"]["predicted_type"] == QUALITY
        assert stored.document.predicted_type == QUALITY
        assert stored.document.prediction_confidence is not None
    else:
        assert stored.document.predicted_type is None
        assert "Document type classifier is unavailable." in stored.warnings
