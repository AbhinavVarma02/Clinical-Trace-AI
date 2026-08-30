"""Train readmission risk models and save the best local artifact.

CLI entry point (``python src/train.py``) and library API (``run_training``) that
preprocesses the data, trains the Logistic Regression / Random Forest / XGBoost
candidates, tunes the decision threshold on validation, selects the best model by
F1 with a recall floor, logs runs to local MLflow, and writes the deployed
``models/`` artifacts consumed by the API and dashboard.

Safety: runs fully locally on public/synthetic data and requires no API keys.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

from src import config
from src.evaluate import (
    calculate_metrics,
    choose_best_threshold,
    classification_report_dict,
    predict_positive_probability,
    save_confusion_matrix_plot,
    save_feature_importance_artifact,
    write_json_artifact,
)
from src.preprocessing import PreprocessingResult, load_raw_data, preprocess_dataset


LOGGER = logging.getLogger(__name__)
MLFLOW_EXPERIMENT_NAME = "clinical-trace-ai-readmission"


def dataset_hash(path: Path) -> str:
    """Calculate a stable hash for the raw data file."""
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# Small, practical XGBoost search. ``spw_mult`` scales the base
# ``scale_pos_weight`` (negatives/positives). The full ratio (mult=1.0) maximizes
# recall but floods false positives; smaller multipliers trade a little recall
# for markedly better precision, F1, and PR-AUC. We search a few points around
# both regularization strength and class weighting.
XGB_PARAM_GRID = [
    {"n_estimators": 300, "max_depth": 4, "learning_rate": 0.03, "subsample": 0.8, "colsample_bytree": 0.8, "min_child_weight": 5, "reg_lambda": 2.0, "reg_alpha": 0.5, "spw_mult": 1.0},
    {"n_estimators": 400, "max_depth": 5, "learning_rate": 0.03, "subsample": 0.85, "colsample_bytree": 0.85, "min_child_weight": 5, "reg_lambda": 2.0, "reg_alpha": 0.5, "spw_mult": 0.5},
    {"n_estimators": 350, "max_depth": 4, "learning_rate": 0.05, "subsample": 0.8, "colsample_bytree": 0.8, "min_child_weight": 10, "reg_lambda": 3.0, "reg_alpha": 1.0, "spw_mult": 0.5},
    {"n_estimators": 400, "max_depth": 5, "learning_rate": 0.03, "subsample": 0.9, "colsample_bytree": 0.8, "min_child_weight": 8, "reg_lambda": 3.0, "reg_alpha": 1.0, "spw_mult": 0.33},
    {"n_estimators": 300, "max_depth": 6, "learning_rate": 0.03, "subsample": 0.85, "colsample_bytree": 0.85, "min_child_weight": 10, "reg_lambda": 4.0, "reg_alpha": 1.0, "spw_mult": 0.5},
    {"n_estimators": 350, "max_depth": 4, "learning_rate": 0.04, "subsample": 0.85, "colsample_bytree": 0.85, "min_child_weight": 8, "reg_lambda": 3.0, "reg_alpha": 1.0, "spw_mult": 0.25},
]

XGB_PARAM_GRID_QUICK = [
    {"n_estimators": 40, "max_depth": 4, "learning_rate": 0.1, "subsample": 0.8, "colsample_bytree": 0.8, "min_child_weight": 3, "reg_lambda": 1.0, "spw_mult": 1.0},
]


def build_model_candidates(y_train: pd.Series, quick: bool = False) -> dict[str, Any]:
    """Create model candidates, including XGBoost with class imbalance handling."""
    positive = int(y_train.sum())
    negative = int(len(y_train) - positive)
    scale_pos_weight = negative / max(positive, 1)
    rf_estimators = 40 if quick else 200

    candidates: dict[str, Any] = {
        "logistic_regression": LogisticRegression(
            class_weight="balanced",
            max_iter=1000,
            solver="liblinear",
            random_state=42,
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=rf_estimators,
            class_weight="balanced",
            min_samples_leaf=2,
            n_jobs=-1,
            random_state=42,
        ),
    }

    try:
        from xgboost import XGBClassifier

        grid = XGB_PARAM_GRID_QUICK if quick else XGB_PARAM_GRID
        for idx, params in enumerate(grid):
            tag = f"xgboost_{idx}" if len(grid) > 1 else "xgboost"
            model_params = {key: value for key, value in params.items() if key != "spw_mult"}
            spw_mult = float(params.get("spw_mult", 1.0))
            candidates[tag] = XGBClassifier(
                **model_params,
                scale_pos_weight=scale_pos_weight * spw_mult,
                objective="binary:logistic",
                eval_metric="aucpr",
                random_state=42,
                n_jobs=-1,
            )
    except Exception as exc:  # pragma: no cover - dependency/environment dependent
        raise RuntimeError("xgboost is required for MVP training.") from exc

    return candidates


def _maybe_start_mlflow(enable_mlflow: bool) -> Any:
    """Configure supported local SQLite tracking when training enables MLflow."""
    if not enable_mlflow:
        return None
    try:
        import mlflow

        config.MLRUNS_DIR.mkdir(parents=True, exist_ok=True)
        database_path = (config.MLRUNS_DIR / "mlflow.db").resolve()
        tracking_uri = f"sqlite:///{database_path.as_posix()}"
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(MLFLOW_EXPERIMENT_NAME)
        LOGGER.info("MLflow tracking enabled at %s", tracking_uri)
        return mlflow
    except Exception as exc:
        message = (
            "MLflow initialization failed while training was configured to track runs. "
            "Disable MLflow explicitly with enable_mlflow=False or correct the local "
            "SQLite tracking configuration."
        )
        LOGGER.exception(message)
        raise RuntimeError(message) from exc


def _log_mlflow_run(
    mlflow_module: Any,
    name: str,
    model: Any,
    metrics: dict[str, Any],
    artifacts_dir: Path,
    data_hash: str,
    all_threshold_results: list[tuple[float, dict[str, Any]]] | None = None,
    val_metrics: dict[str, Any] | None = None,
) -> None:
    if mlflow_module is None:
        return
    with mlflow_module.start_run(run_name=name) as run:
        mlflow_module.log_param("model_type", type(model).__name__)
        mlflow_module.log_param("dataset_hash", data_hash)
        for key, value in model.get_params().items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                mlflow_module.log_param(key, value)
        for key, value in metrics.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                mlflow_module.log_metric(f"test_{key}", float(value))
        if val_metrics:
            for key, value in val_metrics.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    mlflow_module.log_metric(f"val_{key}", float(value))
        if all_threshold_results:
            for threshold, t_metrics in all_threshold_results:
                suffix = f"_t{threshold:.2f}"
                for key in ("precision_positive", "recall_positive", "f1_positive", "f2_positive"):
                    if key in t_metrics:
                        mlflow_module.log_metric(f"{key}{suffix}", float(t_metrics[key]))
        for artifact in artifacts_dir.glob(f"{name}_*"):
            mlflow_module.log_artifact(str(artifact))
        mlflow_module.set_tag("model_role", "candidate")
        mlflow_module.set_tag("run_id", run.info.run_id)


def _select_best(results: list[dict[str, Any]], min_recall: float = 0.65) -> dict[str, Any]:
    """Pick the model/threshold with validation recall >= min_recall and top F1.

    Selection uses *validation* metrics only so the held-out test set stays
    untouched during model and threshold choice. Falls back to highest
    validation F2 if no candidate meets the recall floor.
    """
    eligible = [r for r in results if float(r["val_metrics"]["recall_positive"]) >= min_recall]
    if eligible:
        eligible.sort(key=lambda row: float(row["val_metrics"]["f1_positive"]), reverse=True)
        return eligible[0]
    results.sort(key=lambda row: float(row["val_metrics"]["f2_positive"]), reverse=True)
    return results[0]


def train_models(
    preprocessing: PreprocessingResult,
    artifact_dir: Path,
    data_hash: str,
    quick: bool = False,
    enable_mlflow: bool = True,
) -> dict[str, Any]:
    """Train all model candidates and persist the best performer."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    mlflow_module = _maybe_start_mlflow(enable_mlflow)
    candidates = build_model_candidates(preprocessing.y_train, quick=quick)
    results: list[dict[str, Any]] = []
    artifact_output_dir = (
        config.MLRUNS_DIR / "local_artifacts"
        if artifact_dir.resolve() == config.MODELS_DIR.resolve()
        else artifact_dir
    )
    artifact_output_dir.mkdir(parents=True, exist_ok=True)
    for name, model in candidates.items():
        model.fit(preprocessing.X_train, preprocessing.y_train)

        # Tune the decision threshold on the validation split only.
        val_probabilities = predict_positive_probability(model, preprocessing.X_val)
        tuned_threshold, val_metrics, all_scored = choose_best_threshold(
            preprocessing.y_val,
            val_probabilities,
        )

        # Report honest, held-out performance on the test split at that threshold.
        test_probabilities = predict_positive_probability(model, preprocessing.X_test)
        test_metrics = calculate_metrics(
            preprocessing.y_test,
            test_probabilities,
            tuned_threshold,
        )
        report = classification_report_dict(
            preprocessing.y_test, test_probabilities, tuned_threshold
        )
        metrics_path = artifact_output_dir / f"{name}_metrics.json"
        report_path = artifact_output_dir / f"{name}_classification_report.json"
        cm_path = artifact_output_dir / f"{name}_confusion_matrix.png"
        fi_path = artifact_output_dir / f"{name}_feature_importance.json"
        write_json_artifact(test_metrics, metrics_path)
        write_json_artifact(report, report_path)
        save_confusion_matrix_plot(
            preprocessing.y_test, test_probabilities, tuned_threshold, cm_path
        )
        save_feature_importance_artifact(model, preprocessing.feature_names, fi_path)
        _log_mlflow_run(
            mlflow_module,
            name,
            model,
            test_metrics,
            artifact_output_dir,
            data_hash,
            all_scored,
            val_metrics=val_metrics,
        )
        results.append(
            {
                "name": name,
                "model": model,
                "threshold": tuned_threshold,
                "metrics": test_metrics,
                "val_metrics": val_metrics,
                "probabilities": test_probabilities,
            }
        )

    best = _select_best(results)
    model_version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S-local")
    metadata = {
        "model_type": type(best["model"]).__name__,
        "model_name": best["name"],
        "model_version": model_version,
        "training_date": datetime.now(timezone.utc).date().isoformat(),
        "metrics": best["metrics"],
        "validation_metrics": best["val_metrics"],
        "risk_threshold": float(best["threshold"]),
        "dataset_hash": data_hash,
        "feature_count": len(preprocessing.feature_names),
        "selection_metric": "f1_with_recall_floor_0.65",
        "evaluation_protocol": (
            "Patient-grouped train/validation/test split. Threshold tuned and "
            "model selected on validation only; reported metrics are held-out test."
        ),
        "split_patient_counts": {
            "train": int(preprocessing.train_groups.nunique()),
            "validation": int(preprocessing.val_groups.nunique()),
            "test": int(preprocessing.test_groups.nunique()),
        },
    }

    joblib.dump(best["model"], artifact_dir / "best_model.joblib")
    with (artifact_dir / "model_metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file, indent=2, allow_nan=True)

    if mlflow_module is not None:
        with mlflow_module.start_run(run_name="best_model") as run:
            mlflow_module.log_params(
                {
                    "model_type": metadata["model_type"],
                    "model_name": metadata["model_name"],
                    "dataset_hash": data_hash,
                }
            )
            for key, value in metadata["metrics"].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    mlflow_module.log_metric(key, float(value))
            mlflow_module.log_artifact(str(artifact_dir / "model_metadata.json"))
            mlflow_module.set_tag("model_role", "best")
            mlflow_module.set_tag("model_version", model_version)
            metadata["mlflow_best_run_id"] = run.info.run_id
            with (artifact_dir / "model_metadata.json").open("w", encoding="utf-8") as file:
                json.dump(metadata, file, indent=2, allow_nan=True)

    return metadata


def run_training(
    data_path: Path | str | None = None,
    artifact_dir: Path | None = None,
    processed_dir: Path | None = None,
    ensure_download: bool = True,
    quick: bool = False,
    enable_mlflow: bool = True,
) -> dict[str, Any]:
    """Run preprocessing, train model candidates, and save local artifacts."""
    config.ensure_directories()
    data_path = Path(data_path) if data_path is not None else config.RAW_DATA_PATH
    artifact_dir = artifact_dir or config.MODELS_DIR
    processed_dir = processed_dir or config.PROCESSED_DATA_DIR
    raw_df = load_raw_data(data_path, ensure_download=ensure_download)
    preprocessing = preprocess_dataset(
        raw_df,
        artifact_dir=artifact_dir,
        processed_dir=processed_dir,
        save_artifacts=True,
    )
    hash_value = dataset_hash(data_path) if data_path.exists() else "unknown"
    return train_models(
        preprocessing=preprocessing,
        artifact_dir=artifact_dir,
        data_hash=hash_value,
        quick=quick,
        enable_mlflow=enable_mlflow,
    )


def main() -> None:
    """CLI entry point for `python src/train.py`."""
    metadata = run_training()
    metrics = metadata.get("metrics", {})
    print(
        "Training complete (held-out test metrics). "
        f"Best model: {metadata.get('model_name')} "
        f"recall={metrics.get('recall_positive', 0):.3f} "
        f"precision={metrics.get('precision_positive', 0):.3f} "
        f"f1={metrics.get('f1_positive', 0):.3f} "
        f"pr_auc={metrics.get('pr_auc', 0):.3f} "
        f"roc_auc={metrics.get('roc_auc', 0):.3f} "
        f"threshold={metadata.get('risk_threshold', config.RISK_THRESHOLD):.2f}"
    )


if __name__ == "__main__":
    main()
