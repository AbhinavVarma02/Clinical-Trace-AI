"""Regression tests for user-facing SHAP feature mappings."""

from __future__ import annotations

from src.explainability import rank_features_for_display


def test_inactive_diabetes_medication_category_is_not_displayed():
    ranked = rank_features_for_display(
        [
            {"feature": "diabetesMed No", "contribution": 0.90},
            {"feature": "diabetesMed Yes", "contribution": 0.20},
            {"feature": "number inpatient", "contribution": -0.10},
        ],
        limit=5,
        feature_values={
            "diabetesMed No": 0.0,
            "diabetesMed Yes": 1.0,
            "number inpatient": 0.0,
        },
    )

    labels = [item["display_name"] for item in ranked]
    assert "Diabetes medication status: No" not in labels
    assert "Diabetes medication status: Yes" in labels
    # Numeric zero values remain valid model factors; only inactive one-hot
    # categories are removed.
    assert "Inpatient visits" in labels


def test_inactive_categorical_statuses_are_filtered_before_ranking():
    ranked = rank_features_for_display(
        [
            {"feature": "insulin No", "contribution": 0.80},
            {"feature": "change No", "contribution": 0.70},
            {"feature": "insulin Up", "contribution": 0.15},
            {"feature": "change Ch", "contribution": 0.10},
        ],
        limit=5,
        feature_values={
            "insulin No": 0.0,
            "change No": 0.0,
            "insulin Up": 1.0,
            "change Ch": 1.0,
        },
    )

    labels = [item["display_name"] for item in ranked]
    assert labels == ["Insulin status: Up", "Medication regimen changed"]
