"""Tests for the React + TypeScript SHAP explorer's Streamlit integration."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dashboard.components import shap_explorer
from dashboard.components.shap_explorer import build_explorer_props, explorer_available, render_shap_explorer
from src.config import SAFETY_DISCLAIMER
from src.predict import is_model_loaded


def _result() -> dict:
    return {
        "request_id": "req-123",
        "display_risk_label": "Moderate",
        "prediction": {
            "patient_id": "synthetic_001",
            "readmission_risk": "low",
            "risk_probability": 0.2412,
            "risk_threshold": 0.3,
            "model_version": "test-version",
            "top_features": [
                {"feature": "Inpatient visits", "contribution": -0.1336},
                {"feature": "Number of diagnoses", "contribution": 0.0508},
            ],
        },
        "explanation": {"safety_disclaimer": SAFETY_DISCLAIMER},
    }


def test_props_pass_prediction_values_through_unchanged():
    result = _result()
    props = build_explorer_props(result, SAFETY_DISCLAIMER)
    prediction = result["prediction"]
    assert props == {
        "status": "ready",
        "patientId": "synthetic_001",
        "probability": prediction["risk_probability"],
        "threshold": prediction["risk_threshold"],
        "riskLabel": "low",
        "displayRiskLabel": "Moderate",
        "modelVersion": "test-version",
        "requestId": "req-123",
        "features": prediction["top_features"],
        "disclaimer": SAFETY_DISCLAIMER,
    }
    json.dumps(props, allow_nan=False)


def test_props_for_missing_or_incomplete_results_are_empty_with_disclaimer():
    assert build_explorer_props(None, SAFETY_DISCLAIMER) == {"status": "empty", "disclaimer": SAFETY_DISCLAIMER}
    result = _result()
    result["prediction"]["risk_probability"] = float("nan")
    props = build_explorer_props(result, SAFETY_DISCLAIMER)
    assert props["status"] == "empty"
    assert props["disclaimer"] == SAFETY_DISCLAIMER


def test_props_skip_non_numeric_contributions():
    result = _result()
    result["prediction"]["top_features"].append({"feature": "Broken", "contribution": "n/a"})
    features = build_explorer_props(result, SAFETY_DISCLAIMER)["features"]
    assert [item["feature"] for item in features] == ["Inpatient visits", "Number of diagnoses"]


def test_props_match_typescript_contract():
    """Python keys must stay in sync with the ReadyExplorerData interface."""
    types_ts = Path("frontend/src/types.ts").read_text(encoding="utf-8")
    body = re.search(r"interface ReadyExplorerData \{(.*?)\n\}", types_ts, re.S)
    assert body, "ReadyExplorerData interface not found"
    ts_fields = set(re.findall(r"^\s+(\w+):", body.group(1), re.M))
    assert ts_fields == set(build_explorer_props(_result(), SAFETY_DISCLAIMER))


def test_availability_depends_on_build_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv(shap_explorer.DISABLE_ENV_VAR, raising=False)
    monkeypatch.delenv(shap_explorer.DEV_URL_ENV_VAR, raising=False)
    assert explorer_available(tmp_path) is False
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    assert explorer_available(tmp_path) is True
    monkeypatch.setenv(shap_explorer.DISABLE_ENV_VAR, "1")
    assert explorer_available(tmp_path) is False


def test_render_falls_back_without_touching_streamlit_when_unavailable(monkeypatch):
    monkeypatch.setattr(shap_explorer, "explorer_available", lambda: False)
    monkeypatch.setattr(shap_explorer, "_get_component", lambda: pytest.fail("component must not be declared"))
    assert render_shap_explorer(_result(), SAFETY_DISCLAIMER) is False


def test_render_falls_back_when_component_raises(monkeypatch):
    def broken(**_: object) -> None:
        raise RuntimeError("component failed")

    monkeypatch.setattr(shap_explorer, "explorer_available", lambda: True)
    monkeypatch.setattr(shap_explorer, "_get_component", lambda: broken)
    assert render_shap_explorer(_result(), SAFETY_DISCLAIMER) is False


def test_built_assets_use_relative_paths():
    index = shap_explorer.BUILD_DIR / "index.html"
    if not index.is_file():
        pytest.skip("frontend not built; run `npm run build` in frontend/")
    html = index.read_text(encoding="utf-8")
    assert './assets/' in html
    assert 'src="/assets' not in html and 'href="/assets' not in html


# --------------------------------------------------------------------------- #
# End-to-end: real Streamlit script run with a real synthetic prediction.
# --------------------------------------------------------------------------- #
requires_model = pytest.mark.skipif(not is_model_loaded(), reason="model artifacts not available")


def _run_dashboard_prediction(monkeypatch, disabled: bool):
    from streamlit.testing.v1 import AppTest

    if disabled:
        monkeypatch.setenv(shap_explorer.DISABLE_ENV_VAR, "1")
    else:
        monkeypatch.delenv(shap_explorer.DISABLE_ENV_VAR, raising=False)
    monkeypatch.setattr(shap_explorer, "explorer_available", lambda: not disabled)

    app = AppTest.from_file("dashboard/app.py", default_timeout=120)
    app.run()
    app.button(key="FormSubmitter:patient_risk_simulator-Run risk simulation").click().run()
    app.radio(key="active_section").set_value("Explainability").run()
    assert not app.exception
    return app


@requires_model
def test_dashboard_mounts_explorer_with_stored_prediction(monkeypatch):
    app = _run_dashboard_prediction(monkeypatch, disabled=False)
    result = app.session_state["last_result"]
    instances = app.get("component_instance")
    assert len(instances) == 1
    args = json.loads(instances[0].proto.json_args)["explorer"]
    assert args["status"] == "ready"
    assert args["probability"] == result["prediction"]["risk_probability"]
    assert args["threshold"] == result["prediction"]["risk_threshold"]
    assert args["riskLabel"] == result["prediction"]["readmission_risk"]
    assert args["features"] == result["prediction"]["top_features"]
    assert args["disclaimer"] == SAFETY_DISCLAIMER
    # The accessible contribution table remains available beneath the explorer.
    assert app.expander[0].label == "View contribution values"


@requires_model
def test_dashboard_falls_back_to_static_bars_when_explorer_unavailable(monkeypatch):
    app = _run_dashboard_prediction(monkeypatch, disabled=True)
    assert not app.get("component_instance")
    assert any('class="feat-row"' in markdown.value for markdown in app.markdown)
