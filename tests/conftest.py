"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("CLINICAL_TRACE_SKIP_DOTENV", "1")
import pandas as pd
import pytest


def make_raw_diabetes_frame(rows: int = 36) -> pd.DataFrame:
    """Create a compact UCI-like dataset with patient-level groups."""
    records = []
    for index in range(rows):
        records.append(
            {
                "encounter_id": 10_000 + index,
                "patient_nbr": 20_000 + index,
                "race": "?" if index == 1 else "Caucasian",
                "gender": "Unknown/Invalid" if index == 2 else ("Female" if index % 2 else "Male"),
                "age": "[70-80)" if index % 3 else "[60-70)",
                "weight": "?",
                "admission_type_id": (index % 3) + 1,
                "discharge_disposition_id": 11 if index == 0 else 1,
                "admission_source_id": (index % 4) + 1,
                "time_in_hospital": 2 + (index % 8),
                "payer_code": "?",
                "medical_specialty": "?",
                "num_lab_procedures": 30 + index,
                "num_procedures": index % 4,
                "num_medications": 8 + (index % 20),
                "number_outpatient": index % 3,
                "number_emergency": index % 2,
                "number_inpatient": index % 5,
                "diag_1": "250.13" if index % 4 == 0 else "414",
                "diag_2": "786" if index % 5 == 0 else "401",
                "diag_3": "530" if index % 6 == 0 else "250",
                "number_diagnoses": 4 + (index % 6),
                "max_glu_serum": "None",
                "A1Cresult": ">7" if index % 4 == 0 else "None",
                "metformin": "No",
                "repaglinide": "No",
                "nateglinide": "No",
                "chlorpropamide": "No",
                "glimepiride": "No",
                "acetohexamide": "No",
                "glipizide": "No",
                "glyburide": "No",
                "tolbutamide": "No",
                "pioglitazone": "No",
                "rosiglitazone": "No",
                "acarbose": "No",
                "miglitol": "No",
                "troglitazone": "No",
                "tolazamide": "No",
                "examide": "No",
                "citoglipton": "No",
                "insulin": "Up" if index % 5 == 0 else "No",
                "glyburide-metformin": "No",
                "glipizide-metformin": "No",
                "glimepiride-pioglitazone": "No",
                "metformin-rosiglitazone": "No",
                "metformin-pioglitazone": "No",
                "change": "Ch" if index % 5 == 0 else "No",
                "diabetesMed": "Yes" if index % 3 == 0 else "No",
                "readmitted": "<30" if index % 5 == 0 else (">30" if index % 2 else "NO"),
            }
        )
    return pd.DataFrame(records)


@pytest.fixture()
def raw_diabetes_frame() -> pd.DataFrame:
    return make_raw_diabetes_frame()


@pytest.fixture()
def raw_diabetes_csv(tmp_path: Path, raw_diabetes_frame: pd.DataFrame) -> Path:
    path = tmp_path / "diabetic_data.csv"
    raw_diabetes_frame.to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------- #
# Clinical-Trace AI 2.0 fixtures (synthetic FHIR runtime, in-memory database)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def runtime():
    """Fresh runtime per test: in-memory SQLite, synthetic fixture, no LLM, no ML model."""
    from src.runtime import build_runtime, set_runtime

    rt = build_runtime(":memory:", provider=None, enable_model=False)
    set_runtime(rt)
    yield rt
    set_runtime(None)
    rt.close()


@pytest.fixture()
def api_client(runtime):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.middleware import RATE_LIMITER

    RATE_LIMITER._buckets.clear()
    return TestClient(app)


def persona_headers(persona: str = "clinician.demo") -> dict[str, str]:
    return {"X-Demo-Persona": persona}
