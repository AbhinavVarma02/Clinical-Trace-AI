"""Prediction explainability built around SHAP-compatible outputs.

Computes per-feature contributions (SHAP ``TreeExplainer`` where available, with
a linear/importance fallback) and maps raw encoded feature names to readable,
demo-friendly labels ranked for display. Consumed by ``src.predict`` to fill the
``top_features`` of each response.

Safety: surfaces only model-derived feature contributions, never raw patient
values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.preprocessing import (
    CATEGORICAL_FEATURES,
    GROUPED_CATEGORICAL_SOURCE,
    MEDICATION_COLUMNS,
    humanize_feature_name,
)


# Numeric/utilization features that make clear, demo-friendly explanations.
PRIORITY_NUMERIC_LABELS: dict[str, str] = {
    "time in hospital": "Time in hospital",
    "num lab procedures": "Lab procedures",
    "num procedures": "Procedures performed",
    "num medications": "Number of medications",
    "number outpatient": "Outpatient visits",
    "number emergency": "Emergency visits",
    "number inpatient": "Inpatient visits",
    "number diagnoses": "Number of diagnoses",
    "age ordinal": "Age bracket",
}

# Medication-related columns worth naming individually in an explanation.
_PRIORITY_STATUS_COLUMNS: tuple[str, ...] = ("insulin", "change", "diabetesMed")

# Per-drug columns (aside from insulin) are rarely prescribed and make weak,
# confusing top explanations (e.g. "miglitol No"). Keep them, but push them
# to the bottom of the display ranking rather than dropping the signal.
_RARE_MEDICATION_COLUMNS: tuple[str, ...] = tuple(
    column for column in MEDICATION_COLUMNS if column != "insulin"
)

# Encoded categorical columns are one-hot features. Only the active category
# should be described as a patient factor; an inactive category can have a real
# SHAP contribution, but displaying its label as patient state is misleading.
_ONE_HOT_PREFIXES: tuple[str, ...] = tuple(
    humanize_feature_name(column) for column in CATEGORICAL_FEATURES
) + tuple(
    humanize_feature_name(f"{column}_grouped")
    for column in GROUPED_CATEGORICAL_SOURCE
)


def _match_column(name: str, columns: tuple[str, ...]) -> str | None:
    """Return the column whose encoded name prefixes ``name``, if any."""
    for column in columns:
        if name == column or name.startswith(f"{column} "):
            return column
    return None


def display_feature(raw_name: str) -> tuple[str, int]:
    """Map a raw encoded feature name to a readable label and a priority tier.

    Tier 0 is the demo's preferred, easy-to-read utilization/medication-status
    signals. Tier 1 is everything else (diagnosis categories, labs, admin
    fields). Tier 2 is low-frequency per-drug columns, which are kept but
    deprioritized so they only surface when nothing more useful is available.
    """
    name = str(raw_name).strip()
    if name in PRIORITY_NUMERIC_LABELS:
        return PRIORITY_NUMERIC_LABELS[name], 0

    matched = _match_column(name, _PRIORITY_STATUS_COLUMNS)
    if matched == "insulin":
        value = name[len(matched):].strip() or "recorded"
        return f"Insulin status: {value}", 0
    if matched == "change":
        value = name[len(matched):].strip()
        label = "Medication regimen changed" if value.lower() == "ch" else "No medication regimen change"
        return label, 0
    if matched == "diabetesMed":
        value = name[len(matched):].strip() or "recorded"
        return f"Diabetes medication status: {value}", 0

    matched = _match_column(name, _RARE_MEDICATION_COLUMNS)
    if matched:
        return f"Secondary medication signal: {matched}", 2

    return name, 1


def rank_features_for_display(
    top_features: list[Any],
    limit: int = 5,
    feature_values: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Relabel and rank signals, excluding inactive one-hot categories.

    ``feature_values`` contains the transformed row keyed by encoded feature
    name. When supplied, a categorical one-hot feature is display-eligible only
    when its value is active (1). Numeric features are never filtered this way.
    """
    enriched: list[dict[str, Any]] = []
    for feature in top_features:
        if isinstance(feature, dict):
            raw_name = str(feature.get("feature", "model feature"))
            contribution = float(feature.get("contribution", 0.0))
        elif isinstance(feature, (list, tuple)) and feature:
            raw_name = str(feature[0])
            contribution = float(feature[1]) if len(feature) > 1 else 0.0
        else:
            raw_name = str(getattr(feature, "feature", "model feature"))
            contribution = float(getattr(feature, "contribution", 0.0))

        if feature_values is not None and _match_column(raw_name, _ONE_HOT_PREFIXES):
            if float(feature_values.get(raw_name, 0.0)) < 0.5:
                continue

        display_name, tier = display_feature(raw_name)
        enriched.append(
            {
                "feature": raw_name,
                "display_name": display_name,
                "contribution": contribution,
                "tier": tier,
            }
        )
    enriched.sort(key=lambda item: (item["tier"], -abs(item["contribution"])))
    return enriched[:limit]


def _as_frame(input_row: pd.DataFrame | pd.Series | np.ndarray, feature_names: list[str] | None) -> pd.DataFrame:
    if isinstance(input_row, pd.DataFrame):
        return input_row.iloc[[0]] if len(input_row) != 1 else input_row
    if isinstance(input_row, pd.Series):
        return input_row.to_frame().T
    array = np.asarray(input_row)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    columns = feature_names or [f"feature_{index}" for index in range(array.shape[1])]
    return pd.DataFrame(array, columns=columns)


def _tree_shap_values(model: Any, frame: pd.DataFrame) -> np.ndarray:
    import shap

    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(frame)
    if isinstance(shap_values, list):
        values = np.asarray(shap_values[-1])
    else:
        values = np.asarray(shap_values)
    if values.ndim == 3:
        values = values[:, :, -1]
    return values[0]


def _fallback_contributions(model: Any, frame: pd.DataFrame) -> np.ndarray:
    values = frame.iloc[0].to_numpy(dtype=float)
    if hasattr(model, "coef_"):
        coefficients = np.asarray(model.coef_).ravel()
        return coefficients[: len(values)] * values[: len(coefficients)]
    if hasattr(model, "feature_importances_"):
        importances = np.asarray(model.feature_importances_)
        return importances[: len(values)] * values[: len(importances)]
    return np.zeros(len(values), dtype=float)


def explain_prediction(
    model: Any,
    preprocessed_input: pd.DataFrame | pd.Series | np.ndarray,
    feature_names: list[str] | None = None,
    top_n: int = 5,
) -> list[tuple[str, float]]:
    """Return top feature contributions sorted by absolute value."""
    frame = _as_frame(preprocessed_input, feature_names)
    try:
        values = _tree_shap_values(model, frame)
    except Exception:
        values = _fallback_contributions(model, frame)

    contributions = [
        (str(feature), float(value))
        for feature, value in zip(frame.columns, values, strict=False)
    ]
    contributions.sort(key=lambda item: abs(item[1]), reverse=True)
    return contributions[:top_n]


def generate_shap_summary_plot(
    model: Any,
    X_test: pd.DataFrame,
    output_path: Path,
) -> Path:
    """Generate and save a SHAP summary plot for a tree-based model."""
    import matplotlib.pyplot as plt
    import shap

    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X_test)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shap.summary_plot(shap_values, X_test, show=False)
    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    return output_path
