import hashlib
import json

from src.clinical.model_audit import calibration_summary


def test_calibration_evidence_is_hidden_after_artifact_change(tmp_path):
    model = tmp_path / "best_model.joblib"
    metadata = tmp_path / "model_metadata.json"
    report = tmp_path / "report.json"
    model.write_bytes(b"test-only-artifact-not-a-model")
    metadata.write_text('{"model_version":"test"}')
    report.write_text(
        json.dumps(
            {
                "model_version": "test",
                "provenance": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (model, metadata)},
                "test": {
                    "calibration": {"brier": 0.1, "ece": 0.2, "prevalence": 0.1, "mean_probability": 0.3, "rows": 10}
                },
            }
        )
    )
    assert calibration_summary(report, model, metadata)["rows"] == 10
    model.write_bytes(b"changed")
    assert calibration_summary(report, model, metadata) is None
    assert calibration_summary(tmp_path / "absent.json", model, metadata) is None
