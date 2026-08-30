"""Preprocessing tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.preprocessing import (
    DECEASED_OR_HOSPICE_DISPOSITIONS,
    LONGITUDINAL_FEATURES,
    MODEL_FEATURE_COLUMNS,
    TopNCategoryGrouper,
    compute_longitudinal_features,
    load_raw_data,
    prepare_inference_features,
    preprocess_dataset,
    remove_non_readmission_eligible,
    transform_with_pipeline,
)


def _longitudinal_frame() -> pd.DataFrame:
    """Patient 100 has three encounters (rows shuffled); patient 200 has one."""
    return pd.DataFrame(
        [
            {"patient_nbr": 100, "encounter_id": 30, "number_inpatient": 1, "number_emergency": 0, "time_in_hospital": 6, "readmitted": "NO"},
            {"patient_nbr": 100, "encounter_id": 10, "number_inpatient": 2, "number_emergency": 1, "time_in_hospital": 4, "readmitted": "<30"},
            {"patient_nbr": 200, "encounter_id": 50, "number_inpatient": 9, "number_emergency": 9, "time_in_hospital": 9, "readmitted": "<30"},
            {"patient_nbr": 100, "encounter_id": 20, "number_inpatient": 3, "number_emergency": 0, "time_in_hospital": 8, "readmitted": "<30"},
        ],
        index=[0, 1, 2, 3],
    )


def test_raw_data_loads_question_marks_as_nan(raw_diabetes_csv):
    loaded = load_raw_data(raw_diabetes_csv)
    assert pd.isna(loaded.loc[1, "race"])


def test_deceased_and_hospice_encounters_are_removed(raw_diabetes_frame):
    cleaned = remove_non_readmission_eligible(raw_diabetes_frame)
    assert not cleaned["discharge_disposition_id"].isin(DECEASED_OR_HOSPICE_DISPOSITIONS).any()


def test_preprocessing_outputs_binary_target_and_no_patient_leakage(tmp_path, raw_diabetes_frame):
    result = preprocess_dataset(
        raw_diabetes_frame,
        artifact_dir=tmp_path / "models",
        processed_dir=tmp_path / "processed",
        save_artifacts=True,
        test_size=0.3,
    )

    assert set(result.y_train.unique()).issubset({0, 1})
    assert set(result.y_test.unique()).issubset({0, 1})
    assert "patient_nbr" not in result.X_train.columns
    assert "patient_nbr" not in result.X_test.columns
    assert set(result.train_groups).isdisjoint(set(result.test_groups))
    assert (tmp_path / "models" / "preprocessing_pipeline.joblib").exists()
    assert len(result.feature_names) == result.X_train.shape[1]


def test_pipeline_transforms_single_new_row(tmp_path, raw_diabetes_frame):
    result = preprocess_dataset(
        raw_diabetes_frame,
        artifact_dir=tmp_path / "models",
        processed_dir=tmp_path / "processed",
        save_artifacts=False,
    )
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
        }
    )
    transformed = transform_with_pipeline(result.pipeline, sample)
    assert transformed.shape == (1, result.X_train.shape[1])


def test_missingness_indicators_and_measured_flags():
    records = [
        {
            "patient_id": "present",
            "age": "[70-80)",
            "weight": "[50-75)",
            "payer_code": "MC",
            "medical_specialty": "Cardiology",
            "A1Cresult": ">7",
            "max_glu_serum": ">200",
            "diag_1": "414",
            "diag_2": "250",
            "diag_3": "530",
        },
        {
            "patient_id": "absent",
            "age": "[60-70)",
            # weight / payer_code / medical_specialty intentionally omitted
            "A1Cresult": "None",
            "max_glu_serum": "None",
        },
    ]
    features = prepare_inference_features(records)

    present, absent = features.iloc[0], features.iloc[1]
    assert (present["weight_missing"], present["payer_code_missing"], present["medical_specialty_missing"]) == (0, 0, 0)
    assert (absent["weight_missing"], absent["payer_code_missing"], absent["medical_specialty_missing"]) == (1, 1, 1)
    assert (present["a1c_measured"], present["glucose_measured"]) == (1, 1)
    assert (absent["a1c_measured"], absent["glucose_measured"]) == (0, 0)
    # Raw weight is not kept as a model feature; only its indicator survives.
    assert "weight" not in features.columns


def test_comorbidity_count_counts_distinct_categories():
    records = [
        {"patient_id": "three", "diag_1": "414", "diag_2": "250", "diag_3": "530"},  # circ, diabetes, digestive
        {"patient_id": "one", "diag_1": "414", "diag_2": "410", "diag_3": "420"},  # all circulatory
    ]
    features = prepare_inference_features(records)
    assert int(features.iloc[0]["comorbidity_count"]) == 3
    assert int(features.iloc[1]["comorbidity_count"]) == 1


def test_topn_grouper_learns_from_training_only():
    train = pd.DataFrame(
        {
            "payer_code": ["A", "A", "A", "B", "B", "C", np.nan],
            "medical_specialty": ["X", "X", "Y", "Y", "Z", "Z", np.nan],
        }
    )
    grouper = TopNCategoryGrouper(top_n=2)
    grouper.fit(train)

    # Top-N is derived from training frequencies only.
    assert set(grouper.top_categories_["payer_code"]) == {"A", "B"}

    # A category ("D") never seen in training maps to Other, not a new column.
    new = pd.DataFrame(
        {
            "payer_code": ["A", "C", "D", np.nan],
            "medical_specialty": ["X", "Z", "Q", np.nan],
        }
    )
    out = grouper.transform(new)
    assert list(out["payer_code_grouped"]) == ["A", "Other", "Other", "Missing"]
    assert list(out["medical_specialty_grouped"]) == ["X", "Other", "Other", "Missing"]


def test_grouped_and_new_features_present_and_split_disjoint(raw_diabetes_frame):
    result = preprocess_dataset(
        raw_diabetes_frame,
        save_artifacts=False,
        test_size=0.3,
        val_size=0.25,
    )
    # Raw model feature columns include the new engineered + grouped-source columns.
    assert "comorbidity_count" in MODEL_FEATURE_COLUMNS
    assert "payer_code" in MODEL_FEATURE_COLUMNS and "medical_specialty" in MODEL_FEATURE_COLUMNS

    # Encoded (post-pipeline) frame surfaces the new features and grouped one-hots.
    encoded = set(result.X_train.columns)
    for expected in ("weight missing", "payer code missing", "medical specialty missing", "comorbidity count", "a1c measured", "glucose measured"):
        assert expected in encoded
    assert any("grouped" in column for column in encoded)
    assert result.X_train.shape[1] == len(result.feature_names)
    assert "patient_nbr" not in encoded

    # Patient-level split remains disjoint across all three partitions.
    assert set(result.train_groups).isdisjoint(result.val_groups)
    assert set(result.train_groups).isdisjoint(result.test_groups)
    assert set(result.val_groups).isdisjoint(result.test_groups)


def test_longitudinal_first_encounter_values_are_defaults():
    out = compute_longitudinal_features(_longitudinal_frame())
    # Patient 100's first encounter (encounter_id=10, row index 1) and patient
    # 200's single encounter (row index 2) are both first encounters.
    for idx in (1, 2):
        assert out.loc[idx, "is_first_encounter"] == 1
        assert out.loc[idx, "prior_encounter_count"] == 0
        assert out.loc[idx, "prior_inpatient_count"] == 0
        assert out.loc[idx, "prior_emergency_count"] == 0
        assert out.loc[idx, "prior_readmission_count"] == 0
        assert out.loc[idx, "running_mean_time_in_hospital"] == 0


def test_longitudinal_second_encounter_uses_only_first():
    out = compute_longitudinal_features(_longitudinal_frame())
    # Second encounter of patient 100 is encounter_id=20 (row index 3); it must
    # reflect exactly the first encounter (encounter_id=10), nothing later.
    second = out.loc[3]
    assert second["is_first_encounter"] == 0
    assert second["prior_encounter_count"] == 1
    assert second["prior_inpatient_count"] == 2  # from first encounter only
    assert second["prior_emergency_count"] == 1
    assert second["running_mean_time_in_hospital"] == 4  # first tih only


def test_longitudinal_prior_readmission_excludes_current_row_label():
    out = compute_longitudinal_features(_longitudinal_frame())
    # Second encounter (index 3) is itself readmitted "<30", but its
    # prior_readmission_count must count only the earlier encounter (which was
    # also "<30") — i.e. exactly 1, never 2.
    assert out.loc[3, "prior_readmission_count"] == 1
    # Third encounter (index 0) follows two prior "<30" encounters -> 2.
    assert out.loc[0, "prior_readmission_count"] == 2
    # A first encounter that is itself "<30" (patient 200) must still be 0.
    assert out.loc[2, "prior_readmission_count"] == 0


def test_longitudinal_ordering_is_by_encounter_id_not_row_order():
    frame = _longitudinal_frame()  # rows are intentionally out of encounter order
    out = compute_longitudinal_features(frame)
    # Third encounter by encounter_id (=30, row index 0) must accumulate the two
    # lower encounter_ids (10, 20) despite appearing first in row order.
    assert out.loc[0, "prior_encounter_count"] == 2
    assert out.loc[0, "prior_inpatient_count"] == 5  # 2 + 3
    assert out.loc[0, "running_mean_time_in_hospital"] == 6  # (4 + 8) / 2


def test_longitudinal_inference_defaults_to_first_encounter():
    features = prepare_inference_features({"patient_id": "synthetic_001", "age": "[70-80)", "time_in_hospital": 7})
    row = features.iloc[0]
    assert row["is_first_encounter"] == 1
    for column in LONGITUDINAL_FEATURES:
        if column != "is_first_encounter":
            assert row[column] == 0


def test_longitudinal_inference_preserves_supplied_history():
    features = prepare_inference_features(
        {
            "patient_id": "synthetic_history_001",
            "age": "[70-80)",
            "time_in_hospital": 7,
            "prior_encounter_count": 4,
            "prior_inpatient_count": 3,
            "prior_emergency_count": 2,
            "prior_readmission_count": 1,
            "running_mean_time_in_hospital": 5.5,
            "is_first_encounter": False,
        }
    )
    row = features.iloc[0]
    assert row["prior_encounter_count"] == 4
    assert row["prior_inpatient_count"] == 3
    assert row["prior_emergency_count"] == 2
    assert row["prior_readmission_count"] == 1
    assert row["running_mean_time_in_hospital"] == 5.5
    assert row["is_first_encounter"] == 0


def test_longitudinal_features_feed_model_and_shape_stable(raw_diabetes_frame):
    result = preprocess_dataset(raw_diabetes_frame, save_artifacts=False, test_size=0.3, val_size=0.25)
    encoded = set(result.X_train.columns)
    for expected in ("prior encounter count", "prior readmission count", "running mean time in hospital", "is first encounter"):
        assert expected in encoded
    # Shape is stable across splits and equals the recorded feature count.
    assert result.X_train.shape[1] == result.X_val.shape[1] == result.X_test.shape[1]
    assert result.X_train.shape[1] == len(result.feature_names)
    assert "patient_nbr" not in encoded and "encounter_id" not in encoded
