"""Read-only calibration, threshold, subgroup and error audit of the v1 model.

No estimator is fitted or saved. Raw UCI rows are aligned to saved splits by
reconstructing the original grouped split and verifying *all* matrices/labels.
Only aggregate statistics are exported; no patient/encounter identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import GroupShuffleSplit

from src import config
from src.evaluate import calculate_metrics, choose_best_threshold, predict_positive_probability
from src.persistence.db import utc_now
from src.preprocessing import (
    load_raw_data,
    prepare_model_frame,
    remove_non_readmission_eligible,
    transform_with_pipeline,
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def calibration(y: Any, probability: Any, bins: int = 10) -> dict[str, Any]:
    """Equal-width reliability bins; ECE weighted by bin support, including p=1."""
    labels = np.asarray(y)
    scores = np.asarray(probability, dtype=float)
    if (
        labels.ndim != 1
        or scores.shape != labels.shape
        or not len(labels)
        or not np.isin(labels, [0, 1]).all()
        or not np.isfinite(scores).all()
        or np.any((scores < 0) | (scores > 1))
        or bins < 1
    ):
        raise ValueError("expected nonempty aligned binary labels and finite probabilities in [0,1]")
    membership = np.minimum((scores * bins).astype(int), bins - 1)
    rows = []
    ece = 0.0
    for i in range(bins):
        mask = membership == i
        count = int(mask.sum())
        predicted = float(scores[mask].mean()) if count else None
        observed = float(labels[mask].mean()) if count else None
        if count:
            ece += count / len(scores) * abs(predicted - observed)
        rows.append(
            {
                "lower": i / bins,
                "upper": (i + 1) / bins,
                "count": count,
                "mean_probability": predicted,
                "observed_rate": observed,
            }
        )
    return {
        "brier": float(brier_score_loss(labels, scores)),
        "log_loss": float(log_loss(labels, scores, labels=[0, 1])),
        "ece": ece,
        "bins": rows,
        "prevalence": float(labels.mean()),
        "mean_probability": float(scores.mean()),
        "rows": len(scores),
    }


def finite_metrics(y: Any, scores: Any, threshold: float) -> dict[str, Any]:
    return {
        k: (None if isinstance(v, float) and not np.isfinite(v) else v)
        for k, v in calculate_metrics(y, scores, threshold).items()
    }


def subgroup_report(
    raw: pd.DataFrame, y: Any, probability: Any, threshold: float, min_support: int = 100
) -> list[dict[str, Any]]:
    if min_support < 1 or len(raw) != len(y) or len(y) != len(probability):
        raise ValueError("invalid subgroup support or row alignment")
    labels, scores = np.asarray(y), np.asarray(probability)
    rows = []
    for column in ("age", "race", "gender"):
        if column not in raw:
            rows.append({"attribute": column, "available": False, "reason": "column absent"})
            continue
        values = raw[column].fillna("Missing").astype(str).replace({"?": "Missing"})
        for group in sorted(values.unique()):
            mask = values.eq(group).to_numpy()
            support = int(mask.sum())
            positives = int(labels[mask].sum())
            supported = support >= min_support and 0 < positives < support
            rows.append(
                {
                    "attribute": column,
                    "group": group,
                    "count": support,
                    "positives": positives,
                    "available": supported,
                    "reason": None if supported else "insufficient support or only one outcome class",
                    "metrics": finite_metrics(labels[mask], scores[mask], threshold) if supported else None,
                    "calibration": calibration(labels[mask], scores[mask]) if supported else None,
                }
            )
    return rows


def aligned_splits(
    raw_path: Path, models: Path, processed: Path, metadata: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Fail closed on data/provenance/feature/order mismatches; never refit a pipeline."""
    if file_hash(raw_path) != metadata.get("dataset_hash"):
        raise ValueError("raw dataset hash does not match deployed model metadata")
    raw = load_raw_data(raw_path, ensure_download=False)
    features, y, groups = prepare_model_frame(raw)
    cleaned = remove_non_readmission_eligible(raw)
    trainval, test = next(GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42).split(features, y, groups))
    train_rel, val_rel = next(
        GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42).split(
            features.iloc[trainval], y.iloc[trainval], groups.iloc[trainval]
        )
    )
    indices = {"train": trainval[train_rel], "validation": trainval[val_rel], "test": test}
    pipeline = joblib.load(models / "preprocessing_pipeline.joblib")
    result: dict[str, dict[str, Any]] = {}
    for name, idx in indices.items():
        suffix = "val" if name == "validation" else name
        saved_x = joblib.load(processed / f"X_{suffix}.joblib")
        saved_y = pd.read_csv(processed / f"y_{suffix}.csv").iloc[:, 0].to_numpy()
        computed = transform_with_pipeline(pipeline, features.iloc[idx].reset_index(drop=True))
        if (
            list(computed.columns) != list(saved_x.columns)
            or computed.shape != saved_x.shape
            or not np.allclose(computed, saved_x, rtol=1e-6, atol=1e-7, equal_nan=True)
            or not np.array_equal(y.iloc[idx].to_numpy(), saved_y)
        ):
            raise ValueError(f"saved {name} matrix/labels do not match reconstructed patient split")
        group_values = set(groups.iloc[idx])
        expected = metadata.get("split_patient_counts", {}).get(name)
        if expected is None or len(group_values) != expected:
            raise ValueError(f"patient count mismatch for {name}")
        result[name] = {
            "X": saved_x,
            "y": saved_y,
            "groups": group_values,
            "raw": cleaned.iloc[idx].reset_index(drop=True),
        }
    names = list(result)
    if any(result[a]["groups"] & result[b]["groups"] for i, a in enumerate(names) for b in names[i + 1 :]):
        raise ValueError("patient leakage between partitions")
    return result


def build_report(raw_path: Path, models: Path, processed: Path, min_support: int = 100) -> dict[str, Any]:
    metadata = json.loads((models / "model_metadata.json").read_text())
    splits = aligned_splits(raw_path, models, processed, metadata)
    model = joblib.load(models / "best_model.joblib")
    threshold = float(metadata["risk_threshold"])
    val, test = splits["validation"], splits["test"]
    val_scores = predict_positive_probability(model, val["X"])
    test_scores = predict_positive_probability(model, test["X"])
    # Analysis only; the original deployed threshold is frozen for all test/subgroup results.
    selected, _, sweep = choose_best_threshold(val["y"], val_scores)
    test_metrics = finite_metrics(test["y"], test_scores, threshold)
    for key in ("roc_auc", "pr_auc", "recall_positive", "precision_positive", "f1_positive"):
        if not np.isclose(test_metrics[key], metadata["metrics"][key], atol=1e-6):
            raise ValueError(f"reproduced held-out {key} differs from deployed metadata")
    paths = [
        raw_path,
        models / "best_model.joblib",
        models / "preprocessing_pipeline.joblib",
        models / "model_metadata.json",
        models / "feature_schema.json",
    ]
    paths += [
        processed / f"{prefix}_{split}.{ext}"
        for split in ("train", "val", "test")
        for prefix, ext in (("X", "joblib"), ("y", "csv"))
    ]
    revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "source_commit": revision,
        "model_version": metadata["model_version"],
        "protocol": {
            "dataset": "UCI Diabetes 130-US Hospitals; historical public benchmark, not synthetic FHIR",
            "split": "GroupShuffleSplit seed=42, test=0.2, validation=0.2 of remainder; saved artifacts verified",
            "threshold": threshold,
            "threshold_selection": "validation only; deployed value unchanged",
            "calibration": "analysis of existing probabilities only; no recalibration or retraining",
            "minimum_subgroup_support": min_support,
            "patient_overlap": 0,
        },
        "provenance": {p.name: file_hash(p) for p in paths},
        "packages": {p: importlib.metadata.version(p) for p in ("numpy", "pandas", "scikit-learn", "xgboost")},
        "splits": {name: {"rows": len(s["y"]), "patients": len(s["groups"])} for name, s in splits.items()},
        "validation": {
            "calibration": calibration(val["y"], val_scores),
            "metrics": finite_metrics(val["y"], val_scores, threshold),
            "threshold_sweep": [{"threshold": t, "metrics": m} for t, m in sweep],
            "reproduced_selected_threshold": selected,
        },
        "test": {
            "calibration": calibration(test["y"], test_scores),
            "metrics": test_metrics,
            "subgroups": subgroup_report(test["raw"], test["y"], test_scores, threshold, min_support),
        },
        "limitations": [
            "No clinical validation or causal/fairness conclusions.",
            "Descriptive encounter-level estimates; repeated encounters within patients are correlated.",
            "Small and single-class subgroups are withheld; no confidence intervals or significance claims.",
            "Historical age/race/gender fields have missingness and limited representativeness.",
            "Test data is inspected for reporting only; no changes to models, calibration or thresholds.",
            "A low ECE depends on binning and is not proof of calibration at individual or clinical level.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=config.RAW_DATA_PATH)
    parser.add_argument("--models", type=Path, default=config.MODELS_DIR)
    parser.add_argument("--processed", type=Path, default=config.PROCESSED_DATA_DIR)
    parser.add_argument(
        "--output", type=Path, default=config.PROJECT_ROOT / "evaluation/results/readmission_report.json"
    )
    parser.add_argument("--min-support", type=int, default=100)
    args = parser.parse_args()
    report = build_report(args.raw, args.models, args.processed, args.min_support)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "model_version": report["model_version"],
                "test_calibration": {k: v for k, v in report["test"]["calibration"].items() if k != "bins"},
            }
        )
    )


if __name__ == "__main__":
    main()
