"""FastAPI route tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

import api.routes as routes
from api.main import app
from src import config
from src.config import SAFETY_DISCLAIMER


def _payload() -> dict:
    return {
        "patient_id": "synthetic_001",
        "age": "[70-80)",
        "time_in_hospital": 7,
        "num_lab_procedures": 44,
        "num_procedures": 1,
        "num_medications": 18,
        "number_outpatient": 0,
        "number_emergency": 0,
        "number_inpatient": 2,
        "number_diagnoses": 9,
        "insulin": "Up",
        "change": "Ch",
        "diabetesMed": "Yes",
    }


def _fake_prediction(payload: dict) -> dict:
    return {
        "patient_id": payload["patient_id"],
        "readmission_risk": "high",
        "risk_probability": 0.42,
        "risk_threshold": 0.35,
        "model_version": "test-version",
        "top_features": [{"feature": "number inpatient", "contribution": 0.2}],
    }



def test_root_returns_product_landing_page():
    client = TestClient(app)
    response = client.get("/")
    body = response.text
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Clinical-Trace AI" in body
    assert "Healthcare Readmission Risk Platform with MLOps &amp; LLMOps Observability" in body
    assert "Open API Docs" in body
    assert "Open Streamlit Dashboard" in body
    assert SAFETY_DISCLAIMER in body


def test_favicon_returns_no_content():
    client = TestClient(app)
    response = client.get("/favicon.ico")
    assert response.status_code == 204

def test_health_returns_mode_flags(monkeypatch):
    monkeypatch.setattr(routes, "is_model_loaded", lambda: True)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert {"model_loaded", "llm_mode_active", "langsmith_active"}.issubset(response.json())


def test_model_info_returns_version(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_model_info",
        lambda: {
            "model_type": "XGBClassifier",
            "model_version": "test-version",
            "training_date": "2026-07-03",
            "metrics": {},
            "risk_threshold": 0.35,
        },
    )
    client = TestClient(app)
    response = client.get("/model-info")
    assert response.status_code == 200
    assert response.json()["model_version"] == "test-version"


def test_predict_valid_input(monkeypatch):
    monkeypatch.setattr(routes, "run_prediction", _fake_prediction)
    client = TestClient(app)
    response = client.post("/predict", json=_payload())
    body = response.json()
    assert response.status_code == 200
    assert body["request_id"]
    assert body["readmission_risk"] == "high"
    assert body["risk_probability"] == 0.42
    assert body["top_features"]
    assert body["disclaimer"] == SAFETY_DISCLAIMER
    assert body["model_version"] == "test-version"


def test_predict_passes_optional_longitudinal_history(monkeypatch):
    captured = {}

    def capture_prediction(payload: dict) -> dict:
        captured.update(payload)
        return _fake_prediction(payload)

    payload = {
        **_payload(),
        "prior_encounter_count": 4,
        "prior_inpatient_count": 3,
        "prior_emergency_count": 2,
        "prior_readmission_count": 1,
        "running_mean_time_in_hospital": 5.5,
    }
    monkeypatch.setattr(routes, "run_prediction", capture_prediction)
    response = TestClient(app).post("/predict", json=payload)

    assert response.status_code == 200
    assert captured["prior_encounter_count"] == 4
    assert captured["prior_inpatient_count"] == 3
    assert captured["prior_emergency_count"] == 2
    assert captured["prior_readmission_count"] == 1
    assert captured["running_mean_time_in_hospital"] == 5.5
    assert captured["is_first_encounter"] is False


def test_predict_rejects_first_encounter_with_prior_history():
    payload = {
        **_payload(),
        "prior_encounter_count": 1,
        "is_first_encounter": True,
    }
    response = TestClient(app).post("/predict", json=payload)
    assert response.status_code == 422


def test_predict_missing_required_fields_returns_422():
    client = TestClient(app)
    response = client.post("/predict", json={"patient_id": "synthetic_001"})
    assert response.status_code == 422


def test_explain_without_openai_uses_rule_based(monkeypatch):
    monkeypatch.setattr(routes, "run_prediction", _fake_prediction)
    monkeypatch.setattr(config, "LLM_AVAILABLE", False)
    client = TestClient(app)
    response = client.post("/explain", json=_payload())
    body = response.json()
    assert response.status_code == 200
    assert body["explanation_mode"] == "rule-based"
    assert SAFETY_DISCLAIMER in body["explanation"]
    assert "clinical literature" not in body["explanation"].lower()
    assert "studies show" not in body["explanation"].lower()
