"""Streamlit bridge for the React + TypeScript Risk & SHAP Explainability Explorer.

The explorer is a Streamlit custom component (``components.v1``). Its frontend
lives in ``frontend/`` and is compiled by Vite (``npm run build``) into static
files under ``frontend/dist``, which Streamlit serves itself — Node.js is build
tooling only, never a runtime service.

This module is presentation-only: it reshapes the prediction already stored in
``st.session_state.last_result`` into JSON for the iframe. It runs no inference,
recomputes no SHAP values, and makes no API calls. When the compiled assets are
missing (or the component is disabled), ``render_shap_explorer`` returns
``False`` so the caller can fall back to the original static bars.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = PROJECT_ROOT / "frontend" / "dist"
COMPONENT_NAME = "clinical_trace_shap_explorer"

# Set to "1" to force the original static Streamlit visualization.
DISABLE_ENV_VAR = "CLINICAL_TRACE_DISABLE_REACT_EXPLORER"
# Local frontend development only: point at `npm run dev` (http://localhost:3001).
DEV_URL_ENV_VAR = "CLINICAL_TRACE_EXPLORER_DEV_URL"

_component_func: Callable[..., Any] | None = None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_explorer_props(result: dict[str, Any] | None, disclaimer: str) -> dict[str, Any]:
    """Map an existing dashboard result into the explorer's JSON contract.

    Values are passed through unchanged; only shape and naming differ.
    """
    if not result:
        return {"status": "empty", "disclaimer": disclaimer}

    prediction = result.get("prediction") or {}
    probability = _finite(prediction.get("risk_probability"))
    threshold = _finite(prediction.get("risk_threshold"))
    if probability is None or threshold is None:
        return {
            "status": "empty",
            "disclaimer": disclaimer,
            "message": "Prediction result is unavailable or incomplete.",
        }

    features = []
    for item in prediction.get("top_features") or []:
        if not isinstance(item, dict):
            continue
        contribution = _finite(item.get("contribution"))
        if contribution is None:
            continue
        features.append({"feature": str(item.get("feature", "")), "contribution": contribution})

    return {
        "status": "ready",
        "patientId": str(prediction.get("patient_id", "")),
        "probability": probability,
        "threshold": threshold,
        "riskLabel": str(prediction.get("readmission_risk", "")),
        "displayRiskLabel": result.get("display_risk_label"),
        "modelVersion": str(prediction.get("model_version", "")),
        "requestId": str(result.get("request_id", "")),
        "features": features,
        "disclaimer": disclaimer,
    }


def explorer_available(build_dir: Path = BUILD_DIR) -> bool:
    """Return whether the explorer can be rendered in this environment."""
    if os.environ.get(DISABLE_ENV_VAR, "").strip() == "1":
        return False
    if os.environ.get(DEV_URL_ENV_VAR, "").strip():
        return True
    return (build_dir / "index.html").is_file()


def _get_component() -> Callable[..., Any]:
    global _component_func
    if _component_func is None:
        import streamlit.components.v1 as components

        dev_url = os.environ.get(DEV_URL_ENV_VAR, "").strip()
        if dev_url:
            _component_func = components.declare_component(COMPONENT_NAME, url=dev_url)
        else:
            _component_func = components.declare_component(COMPONENT_NAME, path=str(BUILD_DIR))
    return _component_func


def render_shap_explorer(
    result: dict[str, Any] | None,
    disclaimer: str,
    key: str = "shap_explorer",
) -> bool:
    """Render the explorer; return ``False`` if the caller should fall back."""
    if not explorer_available():
        return False
    try:
        component = _get_component()
        component(explorer=build_explorer_props(result, disclaimer), key=key, default=None)
    except Exception:
        return False
    return True
