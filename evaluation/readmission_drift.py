"""Non-temporal train/test distribution comparison of the preserved UCI splits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib

from evaluation.readmission import aligned_splits, file_hash
from monitoring.drift_report import build_report
from src import config
from src.evaluate import predict_positive_probability


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evaluation/results/readmission_drift.json"))
    args = parser.parse_args()
    metadata = json.loads(config.MODEL_METADATA_PATH.read_text())
    splits = aligned_splits(config.RAW_DATA_PATH, config.MODELS_DIR, config.PROCESSED_DATA_DIR, metadata)
    model = joblib.load(config.MODEL_PATH)
    reference, current = splits["train"], splits["test"]
    columns = [
        "age",
        "race",
        "gender",
        "time_in_hospital",
        "num_medications",
        "num_lab_procedures",
        "number_inpatient",
        "number_emergency",
        "number_outpatient",
    ]
    report = build_report(
        reference["raw"],
        current["raw"],
        columns,
        predict_positive_probability(model, reference["X"]),
        predict_positive_probability(model, current["X"]),
    )
    report["provenance"] = {
        "comparison": "patient-disjoint training versus test, NOT temporal monitoring",
        "dataset_hash": metadata["dataset_hash"],
        "model_hash": file_hash(config.MODEL_PATH),
        "model_version": metadata["model_version"],
        "alignment": "all saved matrices and labels verified",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "prediction_psi": report["predictions"]["psi"],
                "alerts": [k for k, v in report["features"].items() if v.get("alert")],
            }
        )
    )


if __name__ == "__main__":
    main()
