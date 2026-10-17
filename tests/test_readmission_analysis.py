import numpy as np
import pandas as pd
import pytest

from evaluation.readmission import aligned_splits, calibration, file_hash, subgroup_report
from src.preprocessing import load_raw_data, preprocess_dataset


def test_reliability_bins_include_extremes_and_weight_by_support():
    report = calibration([0, 0, 1, 1], [0, 0.25, 0.75, 1], bins=2)
    assert report["brier"] == pytest.approx(0.03125)
    assert report["ece"] == pytest.approx(0.125)
    assert [b["count"] for b in report["bins"]] == [2, 2]
    assert sum(b["count"] for b in report["bins"]) == 4


@pytest.mark.parametrize("labels,scores", [([], []), ([1], [float("nan")]), ([2], [0.5]),
                                            ([1], [1.1]), ([0, 1], [0.5])])
def test_calibration_rejects_invalid_inputs(labels, scores):
    with pytest.raises(ValueError):
        calibration(labels, scores)


def test_missing_and_small_single_class_subgroups_are_explicit():
    raw = pd.DataFrame({"age": ["young", "young", "old"], "race": ["?", "?", "A"]})
    report = subgroup_report(raw, np.array([0, 1, 1]), np.array([0.1, 0.8, 0.9]), 0.5, min_support=2)
    young = next(r for r in report if r.get("group") == "young")
    assert young["metrics"]["confusion_matrix"] == [[1, 0], [0, 1]]
    old = next(r for r in report if r.get("group") == "old")
    assert old["metrics"] is None and old["reason"]
    assert next(r for r in report if r.get("group") == "Missing")["count"] == 2
    assert next(r for r in report if r["attribute"] == "gender")["reason"] == "column absent"


def test_saved_split_alignment_fails_closed_on_row_changes(tmp_path, raw_diabetes_csv):
    raw = load_raw_data(raw_diabetes_csv)
    models, processed = tmp_path / "models", tmp_path / "processed"
    splits = preprocess_dataset(raw, artifact_dir=models, processed_dir=processed)
    metadata = {"dataset_hash": file_hash(raw_diabetes_csv), "split_patient_counts": {
        "train": splits.train_groups.nunique(), "validation": splits.val_groups.nunique(),
        "test": splits.test_groups.nunique(),
    }}
    aligned = aligned_splits(raw_diabetes_csv, models, processed, metadata)
    assert aligned["test"]["raw"].shape[0] == len(splits.y_test)
    labels = pd.read_csv(processed / "y_test.csv")
    labels.iloc[0, 0] = 1 - labels.iloc[0, 0]
    labels.to_csv(processed / "y_test.csv", index=False)
    with pytest.raises(ValueError, match="matrix/labels"):
        aligned_splits(raw_diabetes_csv, models, processed, metadata)
    metadata["dataset_hash"] = "wrong"
    with pytest.raises(ValueError, match="hash"):
        aligned_splits(raw_diabetes_csv, models, processed, metadata)
