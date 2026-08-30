"""Prediction service: load artifacts and produce readmission risk outputs.

Loads the trained model and preprocessing pipeline once per process and runs the
full inference path (feature prep -> pipeline transform -> probability -> risk
label -> top SHAP features), returning the dict consumed by the API and
dashboard. This is the single source of truth for how a prediction is computed.

Safety: rejects non-synthetic patient IDs and never persists request data.
"""

from __future__ import annotations

from typing import Any

import joblib
import numpy as np

from src import config
from src.evaluate import predict_positive_probability
from src.explainability import explain_prediction, rank_features_for_display
from src.preprocessing import prepare_inference_features, transform_with_pipeline
from src.security import is_synthetic_patient_id


_MODEL: Any | None = None
_PIPELINE: Any | None = None


def is_model_loaded() -> bool:
    """Return whether model and pipeline artifacts exist."""
    return config.MODEL_PATH.exists() and config.PIPELINE_PATH.exists()


def load_model_and_pipeline() -> tuple[Any, Any]:
    """Load model and preprocessing pipeline once per process."""
    global _MODEL, _PIPELINE
    if _MODEL is None:
        if not config.MODEL_PATH.exists():
            raise FileNotFoundError("Model artifact missing. Run `python src/train.py` first.")
        _MODEL = joblib.load(config.MODEL_PATH)
    if _PIPELINE is None:
        if not config.PIPELINE_PATH.exists():
            raise FileNotFoundError("Pipeline artifact missing. Run `python src/train.py` first.")
        _PIPELINE = joblib.load(config.PIPELINE_PATH)
    return _MODEL, _PIPELINE


def get_model_info() -> dict[str, Any]:
    """Return deployed model metadata for API and dashboard display."""
    return config.load_model_metadata()


def predict(raw_input: dict[str, Any], top_n: int = 5) -> dict[str, Any]:
    """Run preprocessing, model probability, risk label, and top features."""
    patient_id = str(raw_input.get("patient_id", ""))
    if not is_synthetic_patient_id(patient_id):
        raise ValueError("patient_id must be a synthetic identifier such as synthetic_001.")

    model, pipeline = load_model_and_pipeline()
    metadata = get_model_info()
    threshold = float(metadata.get("risk_threshold", config.configured_risk_threshold()))
    model_version = str(metadata.get("model_version", config.MODEL_VERSION))

    raw_features = prepare_inference_features(raw_input)
    transformed = transform_with_pipeline(pipeline, raw_features)
    probability = float(np.clip(predict_positive_probability(model, transformed)[0], 0.0, 1.0))
    risk_label = "high" if probability >= threshold else "low"
    contributions = explain_prediction(
        model,
        transformed.iloc[[0]],
        feature_names=list(transformed.columns),
        top_n=len(transformed.columns),
    )
    candidate_features = [
        {"feature": feature, "contribution": float(contribution)}
        for feature, contribution in contributions
    ]
    ranked = rank_features_for_display(
        candidate_features,
        limit=top_n,
        feature_values={
            str(feature): float(value)
            for feature, value in transformed.iloc[0].items()
        },
    )
    top_features = [
        {"feature": item["display_name"], "contribution": item["contribution"]}
        for item in ranked
    ]
    return {
        "patient_id": patient_id,
        "readmission_risk": risk_label,
        "risk_probability": probability,
        "risk_threshold": threshold,
        "model_version": model_version,
        "top_features": top_features,
    }
