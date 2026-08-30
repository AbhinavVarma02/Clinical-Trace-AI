"""Model training and prediction tests."""

from __future__ import annotations

import joblib

from src import config
from src.evaluate import predict_positive_probability
from src.preprocessing import prepare_inference_features, transform_with_pipeline
from src.train import MLFLOW_EXPERIMENT_NAME, _maybe_start_mlflow, run_training


def test_training_on_sample_data_produces_model_files(tmp_path, raw_diabetes_csv):
    models_dir = tmp_path / "models"
    metadata = run_training(
        data_path=raw_diabetes_csv,
        artifact_dir=models_dir,
        processed_dir=tmp_path / "processed",
        ensure_download=False,
        quick=True,
        enable_mlflow=False,
    )

    assert (models_dir / "best_model.joblib").exists()
    assert (models_dir / "preprocessing_pipeline.joblib").exists()
    assert metadata["model_type"]
    assert "recall_positive" in metadata["metrics"]


def test_saved_model_returns_probability(tmp_path, raw_diabetes_csv):
    models_dir = tmp_path / "models"
    run_training(
        data_path=raw_diabetes_csv,
        artifact_dir=models_dir,
        processed_dir=tmp_path / "processed",
        ensure_download=False,
        quick=True,
        enable_mlflow=False,
    )
    model = joblib.load(models_dir / "best_model.joblib")
    pipeline = joblib.load(models_dir / "preprocessing_pipeline.joblib")
    sample = prepare_inference_features(
        {
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
            "prior_encounter_count": 4,
            "prior_inpatient_count": 3,
            "prior_emergency_count": 2,
            "prior_readmission_count": 1,
            "running_mean_time_in_hospital": 5.5,
            "is_first_encounter": False,
        }
    )
    assert float(sample.iloc[0]["prior_encounter_count"]) == 4
    assert float(sample.iloc[0]["running_mean_time_in_hospital"]) == 5.5
    assert float(sample.iloc[0]["is_first_encounter"]) == 0
    transformed = transform_with_pipeline(pipeline, sample)
    probability = float(predict_positive_probability(model, transformed)[0])
    assert 0.0 <= probability <= 1.0


def test_mlflow_sqlite_backend_records_run(tmp_path, monkeypatch):
    import mlflow
    from mlflow.tracking import MlflowClient

    previous_tracking_uri = mlflow.get_tracking_uri()
    monkeypatch.setattr(config, "MLRUNS_DIR", tmp_path / "mlruns")
    try:
        mlflow_module = _maybe_start_mlflow(enable_mlflow=True)
        with mlflow_module.start_run(run_name="test-mlflow-run") as run:
            mlflow_module.log_param("verification", "pytest")
            run_id = run.info.run_id

        client = MlflowClient(tracking_uri=mlflow_module.get_tracking_uri())
        experiment = client.get_experiment_by_name(MLFLOW_EXPERIMENT_NAME)
        assert experiment is not None
        runs = client.search_runs([experiment.experiment_id])
        assert any(item.info.run_id == run_id for item in runs)
        assert (config.MLRUNS_DIR / "mlflow.db").exists()
    finally:
        mlflow.set_tracking_uri(previous_tracking_uri)
