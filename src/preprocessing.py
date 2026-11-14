"""Data loading, cleaning, feature engineering, and preprocessing.

Owns UCI Diabetes ingestion, leakage-safe feature engineering, the
patient-grouped train/validation/test split, and the fitted scikit-learn
preprocessing pipeline. Both training (``src.train``) and inference
(``src.predict``) call in here so the exact same transformations apply end to
end.

Safety: splits by ``patient_nbr`` and builds history features from strictly
earlier encounters only, so no patient or future information leaks across splits.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlretrieve

import joblib
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from src import config


UCI_DIABETES_URLS = [
    "https://archive.ics.uci.edu/static/public/296/diabetes+130-us+hospitals+for+years+1999+2008.zip",
    "https://archive.ics.uci.edu/ml/machine-learning-databases/00296/dataset_diabetes.zip",
]

DECEASED_OR_HOSPICE_DISPOSITIONS = {11, 13, 14, 19, 20, 21}
TARGET_COLUMN = "readmitted"
GROUP_COLUMN = "patient_nbr"

# Columns dropped up front (never used, in any form).
DROP_COLUMNS = [
    "encounter_id",
]

# High-missingness columns handled via engineered features instead of being
# dropped outright:
#   - ``weight`` (~97% missing): kept only as a ``weight_missing`` indicator; the
#     raw category itself is discarded.
#   - ``payer_code`` / ``medical_specialty``: kept as train-only Top-N grouped
#     categoricals plus ``*_missing`` indicators.
WEIGHT_COLUMN = "weight"
MISSINGNESS_INDICATOR_SOURCES = {
    "weight": "weight_missing",
    "payer_code": "payer_code_missing",
    "medical_specialty": "medical_specialty_missing",
}
# Raw columns kept for the train-only Top-N grouping branch of the pipeline.
GROUPED_CATEGORICAL_SOURCE = ["payer_code", "medical_specialty"]
PAYER_SPECIALTY_TOP_N = 10

NUMERIC_FEATURES = [
    "time_in_hospital",
    "num_lab_procedures",
    "num_procedures",
    "num_medications",
    "number_outpatient",
    "number_emergency",
    "number_inpatient",
    "number_diagnoses",
]

ID_CATEGORICAL_FEATURES = [
    "admission_type_id",
    "discharge_disposition_id",
    "admission_source_id",
]

DIAGNOSIS_COLUMNS = ["diag_1", "diag_2", "diag_3"]
DIAGNOSIS_CATEGORY_COLUMNS = [f"{column}_category" for column in DIAGNOSIS_COLUMNS]

MEDICATION_COLUMNS = [
    "metformin",
    "repaglinide",
    "nateglinide",
    "chlorpropamide",
    "glimepiride",
    "acetohexamide",
    "glipizide",
    "glyburide",
    "tolbutamide",
    "pioglitazone",
    "rosiglitazone",
    "acarbose",
    "miglitol",
    "troglitazone",
    "tolazamide",
    "examide",
    "citoglipton",
    "insulin",
    "glyburide-metformin",
    "glipizide-metformin",
    "glimepiride-pioglitazone",
    "metformin-rosiglitazone",
    "metformin-pioglitazone",
]

# Medications that are near-constant in the full UCI dataset (>= 99.9% a single
# value). They carry effectively no signal and only add one-hot noise, so they
# are excluded from the model feature set. This is a safe, static list verified
# against the full raw data (each has at most ~40 non-"No" rows out of ~102k).
# They are still accepted at inference time (defaults handle missing columns);
# they are simply not used as model features.
LOW_SIGNAL_MEDICATIONS = [
    "chlorpropamide",
    "acetohexamide",
    "tolbutamide",
    "miglitol",
    "troglitazone",
    "examide",
    "citoglipton",
    "tolazamide",
    "glipizide-metformin",
    "glimepiride-pioglitazone",
    "metformin-rosiglitazone",
    "metformin-pioglitazone",
]

MODEL_MEDICATION_COLUMNS = [
    column for column in MEDICATION_COLUMNS if column not in LOW_SIGNAL_MEDICATIONS
]

CATEGORICAL_FEATURES = [
    "race",
    "gender",
    *ID_CATEGORICAL_FEATURES,
    *DIAGNOSIS_CATEGORY_COLUMNS,
    "max_glu_serum",
    "A1Cresult",
    *MODEL_MEDICATION_COLUMNS,
    "change",
    "diabetesMed",
]

# Simple, clinically motivated engineered numeric features. All are derived
# deterministically in ``prepare_inference_features`` so training and inference
# stay consistent.
ENGINEERED_EXTRA_FEATURES = [
    "total_prior_utilization",
    "emergency_ratio",
    "care_intensity",
    "long_stay_flag",
    "high_medication_flag",
    # Missingness indicators (missingness itself is predictive here).
    "weight_missing",
    "payer_code_missing",
    "medical_specialty_missing",
    # Whether a lab result was actually measured ("None" == not tested).
    "a1c_measured",
    "glucose_measured",
    # Distinct diagnosis categories across diag_1/diag_2/diag_3.
    "comorbidity_count",
]

# Longitudinal patient-history features. Computed per patient using ONLY strictly
# earlier encounters (ordered by ``encounter_id`` as a within-patient sequence
# proxy; no real dates exist so no time-gap features are built). They are leakage
# safe because the split is by ``patient_nbr`` — all of a patient's encounters
# stay in the same partition — and because the current row is excluded via a
# per-patient shift(1). At single-record inference there is no history, so these
# default to first-encounter values (all priors 0, ``is_first_encounter`` = 1).
LONGITUDINAL_FEATURES = [
    "prior_encounter_count",
    "prior_inpatient_count",
    "prior_emergency_count",
    "prior_readmission_count",
    "running_mean_time_in_hospital",
    "is_first_encounter",
]

ENGINEERED_NUMERIC_FEATURES = [
    *NUMERIC_FEATURES,
    "age_ordinal",
    *ENGINEERED_EXTRA_FEATURES,
    *LONGITUDINAL_FEATURES,
]
# The two Top-N grouped source columns are transformed inside the pipeline, so
# they are carried through as raw columns alongside the model feature columns.
MODEL_FEATURE_COLUMNS = [
    *ENGINEERED_NUMERIC_FEATURES,
    *CATEGORICAL_FEATURES,
    *GROUPED_CATEGORICAL_SOURCE,
]

AGE_TO_ORDINAL = {
    "[0-10)": 0,
    "[10-20)": 1,
    "[20-30)": 2,
    "[30-40)": 3,
    "[40-50)": 4,
    "[50-60)": 5,
    "[60-70)": 6,
    "[70-80)": 7,
    "[80-90)": 8,
    "[90-100)": 9,
}

RAW_DEFAULTS: dict[str, Any] = {
    "age": "[70-80)",
    "race": "Unknown",
    "gender": "Unknown",
    "admission_type_id": 0,
    "discharge_disposition_id": 0,
    "admission_source_id": 0,
    "diag_1": "other",
    "diag_2": "other",
    "diag_3": "other",
    "max_glu_serum": "None",
    "A1Cresult": "None",
    "change": "No",
    "diabetesMed": "No",
    # Raw high-missingness sources default to missing at inference; a request
    # that omits them is treated as "not recorded" (indicator = 1, group = Missing).
    "weight": np.nan,
    "payer_code": np.nan,
    "medical_specialty": np.nan,
    **{column: 0 for column in NUMERIC_FEATURES},
    "total_prior_utilization": 0,
    "emergency_ratio": 0.0,
    "care_intensity": 0.0,
    "long_stay_flag": 0,
    "high_medication_flag": 0,
    "weight_missing": 1,
    "payer_code_missing": 1,
    "medical_specialty_missing": 1,
    "a1c_measured": 0,
    "glucose_measured": 0,
    "comorbidity_count": 0,
    # Longitudinal defaults = first-encounter (no prior history available).
    "prior_encounter_count": 0,
    "prior_inpatient_count": 0,
    "prior_emergency_count": 0,
    "prior_readmission_count": 0,
    "running_mean_time_in_hospital": 0.0,
    "is_first_encounter": 1,
    **{column: "No" for column in MEDICATION_COLUMNS},
}

NOT_MEASURED_VALUES = {"None", "none", "NONE"}

LONG_STAY_DAYS = 7
HIGH_MEDICATION_COUNT = 20


@dataclass(frozen=True)
class PreprocessingResult:
    """Container for split data and fitted preprocessing artifacts."""

    X_train: pd.DataFrame
    X_val: pd.DataFrame
    X_test: pd.DataFrame
    y_train: pd.Series
    y_val: pd.Series
    y_test: pd.Series
    train_groups: pd.Series
    val_groups: pd.Series
    test_groups: pd.Series
    pipeline: ColumnTransformer
    raw_train: pd.DataFrame
    raw_val: pd.DataFrame
    raw_test: pd.DataFrame
    feature_names: list[str]


def download_dataset(raw_dir: Path | None = None, force: bool = False) -> None:
    """Download the public UCI Diabetes dataset into data/raw."""
    raw_dir = raw_dir or config.RAW_DATA_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    csv_path = raw_dir / "diabetic_data.csv"
    mapping_path = raw_dir / "IDS_mapping.csv"
    if csv_path.exists() and mapping_path.exists() and not force:
        return

    zip_path = raw_dir / "uci_diabetes.zip"
    last_error: Exception | None = None
    for url in UCI_DIABETES_URLS:
        try:
            urlretrieve(url, zip_path)
            last_error = None
            break
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
    if last_error is not None:
        raise RuntimeError(
            "Unable to download the UCI Diabetes dataset. "
            "Place diabetic_data.csv and IDS_mapping.csv in data/raw/ manually."
        ) from last_error

    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():
            lower_name = member.lower()
            if lower_name.endswith("diabetic_data.csv"):
                with archive.open(member) as source, csv_path.open("wb") as target:
                    target.write(source.read())
            elif lower_name.endswith("ids_mapping.csv"):
                with archive.open(member) as source, mapping_path.open("wb") as target:
                    target.write(source.read())

    if not csv_path.exists():
        raise FileNotFoundError("Downloaded archive did not contain diabetic_data.csv.")


def load_raw_data(
    path: Path | str | None = None,
    ensure_download: bool = False,
) -> pd.DataFrame:
    """Load the raw UCI CSV with '?' interpreted as missing values."""
    path = Path(path) if path is not None else config.RAW_DATA_PATH
    if not path.exists() and ensure_download:
        download_dataset(path.parent)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing raw dataset at {path}. Place diabetic_data.csv in data/raw/ "
            "or run with ensure_download=True."
        )
    return pd.read_csv(path, na_values=["?"], low_memory=False)


def remove_non_readmission_eligible(df: pd.DataFrame) -> pd.DataFrame:
    """Remove encounters that ended in death or hospice discharge."""
    if "discharge_disposition_id" not in df.columns:
        return df.copy()
    mask = ~df["discharge_disposition_id"].isin(DECEASED_OR_HOSPICE_DISPOSITIONS)
    return df.loc[mask].copy()


def encode_target(df: pd.DataFrame) -> pd.Series:
    """Encode readmission within 30 days as the positive class."""
    if TARGET_COLUMN not in df.columns:
        raise KeyError(f"Expected target column '{TARGET_COLUMN}' in raw data.")
    return df[TARGET_COLUMN].eq("<30").astype(int)


def map_icd9_to_category(value: Any) -> str:
    """Map ICD-9 codes to broad clinical categories for model features."""
    if pd.isna(value):
        return "missing"
    text = str(value).strip()
    if not text:
        return "missing"
    if text[0].upper() in {"E", "V"}:
        return "other"
    try:
        code = float(text)
    except ValueError:
        return "other"

    if 390 <= code <= 459 or int(code) == 785:
        return "circulatory"
    if 460 <= code <= 519 or int(code) == 786:
        return "respiratory"
    if int(code) == 250:
        return "diabetes"
    if 520 <= code <= 579 or int(code) == 787:
        return "digestive"
    if 800 <= code <= 999:
        return "injury"
    if 710 <= code <= 739:
        return "musculoskeletal"
    if 580 <= code <= 629 or int(code) == 788:
        return "genitourinary"
    if 140 <= code <= 239:
        return "neoplasms"
    return "other"


def _frame_from_records(records: dict[str, Any] | list[dict[str, Any]] | pd.DataFrame) -> pd.DataFrame:
    if isinstance(records, pd.DataFrame):
        return records.copy()
    if isinstance(records, dict):
        return pd.DataFrame([records])
    return pd.DataFrame(records)


def _ensure_raw_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    supplied_first_encounter = result.get("is_first_encounter")
    for column, default in RAW_DEFAULTS.items():
        if column not in result.columns:
            result[column] = default

    history_values = result[
        [column for column in LONGITUDINAL_FEATURES if column != "is_first_encounter"]
    ].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    inferred_first_encounter = history_values.eq(0.0).all(axis=1).astype(int)
    if supplied_first_encounter is None:
        result["is_first_encounter"] = inferred_first_encounter
    else:
        explicit = pd.to_numeric(supplied_first_encounter, errors="coerce")
        result["is_first_encounter"] = explicit.fillna(inferred_first_encounter)
    return result


def prepare_inference_features(
    records: dict[str, Any] | list[dict[str, Any]] | pd.DataFrame,
) -> pd.DataFrame:
    """Prepare raw API-style records for a fitted preprocessing pipeline."""
    features = _ensure_raw_columns(_frame_from_records(records))
    features = features.drop(columns=[*DROP_COLUMNS, TARGET_COLUMN, GROUP_COLUMN, "patient_id"], errors="ignore")

    features["gender"] = features["gender"].replace({"Unknown/Invalid": np.nan})
    features["age_ordinal"] = features["age"].map(AGE_TO_ORDINAL)
    features = features.drop(columns=["age"], errors="ignore")

    # Missingness indicators. Normalize "?"/empty to NaN first so both raw-CSV
    # ("?") and API-style records are treated identically.
    for source_col, indicator_col in MISSINGNESS_INDICATOR_SOURCES.items():
        if source_col not in features.columns:
            features[source_col] = np.nan
        features[source_col] = features[source_col].replace({"?": np.nan, "": np.nan})
        features[indicator_col] = features[source_col].isna().astype(int)

    # "Measured" flags: A1Cresult / max_glu_serum use the literal "None" to mean
    # the lab was not run, which correlates with workup intensity.
    for source_col, measured_col in (("A1Cresult", "a1c_measured"), ("max_glu_serum", "glucose_measured")):
        if source_col not in features.columns:
            features[source_col] = RAW_DEFAULTS.get(source_col, "None")
        values = features[source_col].astype("object")
        features[measured_col] = (
            values.notna() & ~values.isin(NOT_MEASURED_VALUES)
        ).astype(int)

    # ``weight`` is kept only via its missingness indicator; drop the raw column.
    features = features.drop(columns=[WEIGHT_COLUMN], errors="ignore")

    for col in (
        "number_inpatient",
        "number_outpatient",
        "number_emergency",
        "num_medications",
        "num_procedures",
        "time_in_hospital",
    ):
        if col not in features.columns:
            features[col] = 0

    inpatient = pd.to_numeric(features["number_inpatient"], errors="coerce").fillna(0)
    outpatient = pd.to_numeric(features["number_outpatient"], errors="coerce").fillna(0)
    emergency = pd.to_numeric(features["number_emergency"], errors="coerce").fillna(0)
    num_medications = pd.to_numeric(features["num_medications"], errors="coerce").fillna(0)
    num_procedures = pd.to_numeric(features["num_procedures"], errors="coerce").fillna(0)
    time_in_hospital = pd.to_numeric(features["time_in_hospital"], errors="coerce").fillna(0)

    # Total prior-year utilization across care settings.
    features["total_prior_utilization"] = inpatient + outpatient + emergency
    # Share of prior utilization that was via the emergency department.
    features["emergency_ratio"] = emergency / (features["total_prior_utilization"] + 1.0)
    # Treatment burden per inpatient day.
    features["care_intensity"] = (num_medications + num_procedures) / (time_in_hospital + 1.0)
    # Binary flags for clinically meaningful thresholds.
    features["long_stay_flag"] = (time_in_hospital >= LONG_STAY_DAYS).astype(int)
    features["high_medication_flag"] = (num_medications >= HIGH_MEDICATION_COUNT).astype(int)

    for diag_column in DIAGNOSIS_COLUMNS:
        source = features[diag_column] if diag_column in features.columns else pd.Series(np.nan, index=features.index)
        features[f"{diag_column}_category"] = source.map(map_icd9_to_category)

    # Comorbidity count: number of distinct *real* diagnosis categories across
    # diag_1/2/3. "missing" (unmapped/absent codes) is excluded from the count.
    diagnosis_categories = features[DIAGNOSIS_CATEGORY_COLUMNS].where(
        features[DIAGNOSIS_CATEGORY_COLUMNS] != "missing"
    )
    features["comorbidity_count"] = diagnosis_categories.nunique(axis=1).astype(int)

    features = features.drop(columns=DIAGNOSIS_COLUMNS, errors="ignore")

    for column in CATEGORICAL_FEATURES:
        if column not in features.columns:
            features[column] = RAW_DEFAULTS.get(column, "Unknown")
        features[column] = features[column].astype("object")

    for column in ENGINEERED_NUMERIC_FEATURES:
        if column not in features.columns:
            features[column] = RAW_DEFAULTS.get(column, np.nan)
        features[column] = pd.to_numeric(features[column], errors="coerce")

    # Raw Top-N grouping source columns are transformed inside the pipeline; keep
    # them as object so the fitted grouper/encoder handle unseen and missing values.
    for column in GROUPED_CATEGORICAL_SOURCE:
        if column not in features.columns:
            features[column] = np.nan
        features[column] = features[column].replace({"?": np.nan, "": np.nan}).astype("object")

    return features[MODEL_FEATURE_COLUMNS].copy()


def compute_longitudinal_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute per-patient prior-history features using only earlier encounters.

    Encounters are ordered within each patient by ``encounter_id`` (a defensible
    sequence proxy — it is monotonic within every patient — but NOT a time gap;
    no date columns exist). Every "prior" feature excludes the current row via a
    per-patient ``shift(1)``, so a row's own values and its own ``readmitted``
    label are never used. Returns a frame indexed like ``df`` with
    ``LONGITUDINAL_FEATURES`` columns. If ordering columns are unavailable, all
    rows fall back to first-encounter defaults.
    """
    defaults = {
        "prior_encounter_count": 0.0,
        "prior_inpatient_count": 0.0,
        "prior_emergency_count": 0.0,
        "prior_readmission_count": 0.0,
        "running_mean_time_in_hospital": 0.0,
        "is_first_encounter": 1.0,
    }
    if GROUP_COLUMN not in df.columns or "encounter_id" not in df.columns:
        return pd.DataFrame({name: value for name, value in defaults.items()}, index=df.index)

    numeric = pd.DataFrame(
        {
            "pt": df[GROUP_COLUMN].values,
            "eid": pd.to_numeric(df["encounter_id"], errors="coerce").values,
            "inpatient": pd.to_numeric(df.get("number_inpatient"), errors="coerce").fillna(0).values,
            "emergency": pd.to_numeric(df.get("number_emergency"), errors="coerce").fillna(0).values,
            "tih": pd.to_numeric(df.get("time_in_hospital"), errors="coerce").fillna(0).values,
            "readmit30": df[TARGET_COLUMN].eq("<30").astype(int).values
            if TARGET_COLUMN in df.columns
            else 0,
        },
        index=df.index,
    )

    # Stable sort by patient then encounter_id so ties keep input order.
    order = numeric.sort_values(["pt", "eid"], kind="mergesort")
    grouped = order.groupby("pt", sort=False)
    first_in_group = grouped.cumcount() == 0

    def prior_sum(column: str) -> pd.Series:
        # Inclusive cumulative sum shifted by one row = sum over strictly earlier
        # encounters. shift(1) crosses the group boundary at each patient's first
        # row, so those are reset to 0 via the first_in_group mask.
        prior = grouped[column].cumsum().shift(1)
        prior[first_in_group] = 0.0
        return prior

    out = pd.DataFrame(index=order.index)
    out["prior_encounter_count"] = grouped.cumcount().astype(float)
    out["prior_inpatient_count"] = prior_sum("inpatient").astype(float)
    out["prior_emergency_count"] = prior_sum("emergency").astype(float)
    out["prior_readmission_count"] = prior_sum("readmit30").astype(float)
    prior_tih_sum = prior_sum("tih")
    out["running_mean_time_in_hospital"] = np.where(
        out["prior_encounter_count"] > 0,
        prior_tih_sum / out["prior_encounter_count"].replace(0, np.nan),
        0.0,
    )
    out["is_first_encounter"] = first_in_group.astype(float)

    return out.reindex(df.index)


def prepare_model_frame(raw_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Clean raw data and return model features, target, and patient groups."""
    cleaned = remove_non_readmission_eligible(raw_df)
    y = encode_target(cleaned)
    if GROUP_COLUMN not in cleaned.columns:
        raise KeyError(f"Expected grouping column '{GROUP_COLUMN}' in raw data.")
    groups = cleaned[GROUP_COLUMN].copy()
    features = prepare_inference_features(cleaned)
    # Overwrite the first-encounter defaults with real per-patient history.
    longitudinal = compute_longitudinal_features(cleaned)
    for column in LONGITUDINAL_FEATURES:
        features[column] = longitudinal[column].reindex(features.index).astype(float).values
    return features, y, groups


class TopNCategoryGrouper(BaseEstimator, TransformerMixin):
    """Group high-cardinality categoricals into Top-N + Other + Missing.

    The Top-N categories are learned in ``fit`` from the training data only, so
    no validation/test information leaks into the grouping. In ``transform``:
    missing values map to ``missing_label``, values outside the learned Top-N
    map to ``other_label``, and the rest are kept as-is. Output column names are
    ``<column>_grouped`` so downstream encoders and displays stay readable.
    """

    def __init__(self, top_n: int = 10, other_label: str = "Other", missing_label: str = "Missing") -> None:
        self.top_n = top_n
        self.other_label = other_label
        self.missing_label = missing_label

    def _to_frame(self, X: Any) -> pd.DataFrame:
        if isinstance(X, pd.DataFrame):
            return X
        columns = getattr(self, "columns_", None)
        return pd.DataFrame(X, columns=columns)

    def fit(self, X: Any, y: Any = None) -> "TopNCategoryGrouper":
        frame = self._to_frame(X)
        self.columns_ = list(frame.columns)
        self.top_categories_: dict[str, list[str]] = {}
        for column in self.columns_:
            counts = frame[column].replace({"?": np.nan, "": np.nan}).dropna().value_counts()
            self.top_categories_[column] = list(counts.head(self.top_n).index)
        return self

    def transform(self, X: Any) -> pd.DataFrame:
        frame = self._to_frame(X)
        output = pd.DataFrame(index=frame.index)
        for column in self.columns_:
            series = frame[column].replace({"?": np.nan, "": np.nan})
            top = set(self.top_categories_[column])
            grouped = series.where(series.isin(top), other=self.other_label)
            grouped = grouped.mask(series.isna(), self.missing_label)
            output[f"{column}_grouped"] = grouped.astype("object")
        return output

    def get_feature_names_out(self, input_features: Any = None) -> np.ndarray:
        return np.asarray([f"{column}_grouped" for column in self.columns_], dtype=object)


def build_preprocessing_pipeline() -> ColumnTransformer:
    """Build the fitted-on-train-only preprocessing transformer."""
    numeric_pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
        ]
    )
    categorical_pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="most_frequent")),
            (
                "onehot",
                OneHotEncoder(
                    handle_unknown="ignore",
                    sparse_output=False,
                    dtype=np.float32,
                ),
            ),
        ]
    )
    # Top-N grouping (fit on train only) followed by one-hot encoding. The
    # grouper already maps NaN -> "Missing", so no imputer is needed here.
    grouped_pipeline = Pipeline(
        steps=[
            ("group", TopNCategoryGrouper(top_n=PAYER_SPECIALTY_TOP_N)),
            (
                "onehot",
                OneHotEncoder(
                    handle_unknown="ignore",
                    sparse_output=False,
                    dtype=np.float32,
                ),
            ),
        ]
    )
    return ColumnTransformer(
        transformers=[
            ("num", numeric_pipeline, ENGINEERED_NUMERIC_FEATURES),
            ("cat", categorical_pipeline, CATEGORICAL_FEATURES),
            ("grouped", grouped_pipeline, GROUPED_CATEGORICAL_SOURCE),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )


def transform_with_pipeline(
    pipeline: ColumnTransformer,
    feature_frame: pd.DataFrame,
) -> pd.DataFrame:
    """Transform features and return a named DataFrame."""
    transformed = pipeline.transform(feature_frame)
    if hasattr(transformed, "toarray"):
        transformed = transformed.toarray()
    feature_names = [humanize_feature_name(name) for name in pipeline.get_feature_names_out()]
    return pd.DataFrame(transformed, columns=feature_names, index=feature_frame.index).astype(np.float32)


def humanize_feature_name(name: str) -> str:
    """Make encoded feature names suitable for API and dashboard display."""
    cleaned = name.replace("__", "_").replace("_", " ")
    cleaned = cleaned.replace("diag 1", "primary diagnosis")
    cleaned = cleaned.replace("diag 2", "secondary diagnosis")
    cleaned = cleaned.replace("diag 3", "tertiary diagnosis")
    return cleaned.strip()


def preprocess_dataset(
    raw_df: pd.DataFrame,
    artifact_dir: Path | None = None,
    processed_dir: Path | None = None,
    save_artifacts: bool = True,
    test_size: float = 0.2,
    val_size: float = 0.2,
    random_state: int = 42,
) -> PreprocessingResult:
    """Create patient-grouped train/val/test splits, fit preprocessing on train.

    The split is done in two grouped stages so that no ``patient_nbr`` appears in
    more than one partition:

    1. Hold out ``test_size`` of patients as the untouched test set.
    2. From the remaining patients, hold out ``val_size`` (relative to that
       remainder) as a validation set used only for threshold tuning and model
       selection. The test set is never used to make those decisions.
    """
    artifact_dir = artifact_dir or config.MODELS_DIR
    processed_dir = processed_dir or config.PROCESSED_DATA_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    features, y, groups = prepare_model_frame(raw_df)

    test_splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state)
    trainval_idx, test_idx = next(test_splitter.split(features, y, groups=groups))

    trainval_features = features.iloc[trainval_idx]
    trainval_y = y.iloc[trainval_idx]
    trainval_groups = groups.iloc[trainval_idx]

    val_splitter = GroupShuffleSplit(n_splits=1, test_size=val_size, random_state=random_state)
    rel_train_idx, rel_val_idx = next(
        val_splitter.split(trainval_features, trainval_y, groups=trainval_groups)
    )
    train_idx = trainval_idx[rel_train_idx]
    val_idx = trainval_idx[rel_val_idx]

    raw_train = features.iloc[train_idx].reset_index(drop=True)
    raw_val = features.iloc[val_idx].reset_index(drop=True)
    raw_test = features.iloc[test_idx].reset_index(drop=True)
    y_train = y.iloc[train_idx].reset_index(drop=True)
    y_val = y.iloc[val_idx].reset_index(drop=True)
    y_test = y.iloc[test_idx].reset_index(drop=True)
    train_groups = groups.iloc[train_idx].reset_index(drop=True)
    val_groups = groups.iloc[val_idx].reset_index(drop=True)
    test_groups = groups.iloc[test_idx].reset_index(drop=True)

    pipeline = build_preprocessing_pipeline()
    pipeline.fit(raw_train)

    X_train = transform_with_pipeline(pipeline, raw_train)
    X_val = transform_with_pipeline(pipeline, raw_val)
    X_test = transform_with_pipeline(pipeline, raw_test)
    feature_names = list(X_train.columns)

    result = PreprocessingResult(
        X_train=X_train,
        X_val=X_val,
        X_test=X_test,
        y_train=y_train,
        y_val=y_val,
        y_test=y_test,
        train_groups=train_groups,
        val_groups=val_groups,
        test_groups=test_groups,
        pipeline=pipeline,
        raw_train=raw_train,
        raw_val=raw_val,
        raw_test=raw_test,
        feature_names=feature_names,
    )

    if save_artifacts:
        joblib.dump(pipeline, artifact_dir / "preprocessing_pipeline.joblib")
        joblib.dump(X_train, processed_dir / "X_train.joblib")
        joblib.dump(X_val, processed_dir / "X_val.joblib")
        joblib.dump(X_test, processed_dir / "X_test.joblib")
        y_train.to_csv(processed_dir / "y_train.csv", index=False)
        y_val.to_csv(processed_dir / "y_val.csv", index=False)
        y_test.to_csv(processed_dir / "y_test.csv", index=False)
        schema = {
            "raw_feature_columns": MODEL_FEATURE_COLUMNS,
            "numeric_features": ENGINEERED_NUMERIC_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
            "grouped_categorical_source": GROUPED_CATEGORICAL_SOURCE,
            "grouped_top_n": PAYER_SPECIALTY_TOP_N,
            "encoded_feature_names": feature_names,
            "train_patient_count": int(train_groups.nunique()),
            "val_patient_count": int(val_groups.nunique()),
            "test_patient_count": int(test_groups.nunique()),
        }
        with (artifact_dir / "feature_schema.json").open("w", encoding="utf-8") as file:
            json.dump(schema, file, indent=2)

    return result
