"""Compatibility bridge between synthetic FHIR patients and the UCI readmission model.

The readmission model was trained on the UCI Diabetes 130-US Hospitals dataset
(diabetic *inpatient* encounters, 1999-2008, ICD-9 coded). It must not be
applied to records it cannot represent. This module therefore:

1. checks eligibility (diabetes diagnosis, a completed inpatient encounter,
   a recorded discharge time, a medication list);
2. maps eligible encounters onto the model's input schema, listing every
   approximation it makes (ICD-10 -> ICD-9 category, counts derived from
   synthetic resources);
3. otherwise returns ``available: false`` with explicit reasons.

The output is flagged as out-of-distribution relative to the training data.
"""

from __future__ import annotations

from typing import Any

from src.clinical.fhir_models import parse_timestamp
from src.clinical.records import ClinicalRecords

# ICD-10 prefix -> representative ICD-9 code that maps to the same broad
# category in ``src.preprocessing.map_icd9_to_category``.
ICD10_TO_ICD9_CATEGORY_CODE = (
    ("E11", "250"),   # diabetes
    ("E10", "250"),
    ("I", "428"),     # circulatory
    ("J", "496"),     # respiratory
    ("K", "530"),     # digestive
    ("N", "585"),     # genitourinary
    ("C", "174"),     # neoplasms
    ("M", "715"),     # musculoskeletal
    ("S", "850"),     # injury
    ("T", "850"),
)
DIABETES_MED_KEYWORDS = ("metformin", "insulin", "glipizide", "glyburide", "glimepiride", "pioglitazone",
                         "sitagliptin")
ADMISSION_TYPE = {"Emergency department": 1}
ADMISSION_SOURCE = {"Emergency department": 7}


def icd10_to_icd9_proxy(code: str) -> str:
    for prefix, icd9 in ICD10_TO_ICD9_CATEGORY_CODE:
        if code.upper().startswith(prefix):
            return icd9
    return "V00"  # maps to "other"


def _age_bracket(years: int) -> str:
    low = min(max(years // 10 * 10, 0), 90)
    return f"[{low}-{low + 10})"


def _a1c_category(value: float | None) -> str:
    if value is None:
        return "None"
    if value > 8:
        return ">8"
    if value > 7:
        return ">7"
    return "Norm"


def _glucose_category(value: float | None) -> str:
    if value is None:
        return "None"
    if value > 300:
        return ">300"
    if value > 200:
        return ">200"
    return "Norm"


def assess(records: ClinicalRecords, patient_id: str, encounter_id: str | None = None) -> dict[str, Any]:
    """Return model eligibility, mapped features, and caveats for one encounter."""
    profile = records.profile(patient_id)
    if profile is None:
        return {"available": False, "reasons": ["patient not found"], "patient_id": patient_id}
    conditions = records.conditions(patient_id)
    encounters = records.encounters(patient_id)
    inpatient = [e for e in encounters if e["class"] == "IMP"]
    reasons: list[str] = []
    if not any(c["code"].upper().startswith(("E10", "E11")) for c in conditions):
        reasons.append("Model population is diabetic inpatient encounters; no diabetes diagnosis is recorded.")
    if not inpatient:
        reasons.append("No inpatient encounter is recorded.")
    target = None
    if inpatient:
        if encounter_id:
            target = next((e for e in inpatient if e["id"] == encounter_id), None)
            if target is None:
                reasons.append(f"Encounter {encounter_id} is not an inpatient encounter for this patient.")
        else:
            target = inpatient[-1]
    if target is not None and not target["end"]:
        reasons.append(f"Encounter {target['id']} has no discharge time, so length of stay is unknown.")
    medications = records.medications(patient_id)
    if not medications:
        reasons.append("No medication orders are recorded, so medication counts cannot be derived.")
    if reasons or target is None:
        return {
            "available": False,
            "patient_id": patient_id,
            "encounter_id": target["id"] if target else None,
            "reasons": reasons,
        }

    start = parse_timestamp(target["start"])
    end = parse_timestamp(target["end"])
    window_start = start.replace(year=start.year - 1)
    prior = [e for e in encounters if e["start"] and parse_timestamp(e["start"]) < start]
    prior_year = [e for e in prior if parse_timestamp(e["start"]) >= window_start]
    observations = [o for o in records.observations(patient_id) if o["encounter"] == f"Encounter/{target['id']}"]
    labs = [o for o in observations if o["category"] == "laboratory"]

    def latest_value(loinc: str) -> float | None:
        values = [o["value"] for o in labs if o["loinc"] == loinc and isinstance(o["value"], (int, float))]
        return float(values[-1]) if values else None

    def effective(version: dict[str, Any]) -> Any:
        # Version 1 takes effect when ordered; later versions when they were recorded.
        if version["source"]["version"] == "1" or not version["source"]["last_updated"]:
            return parse_timestamp(version["authored_on"])
        return parse_timestamp(version["source"]["last_updated"])

    def active_at(med: dict[str, Any]) -> bool:
        in_force = [v for v in med["history"] if effective(v) <= end]
        if not in_force:
            return False
        version = in_force[-1]
        return version["status"] == "active" or (version["status"] == "stopped" and effective(version) >= start)

    meds_at_encounter = [m for m in medications if active_at(m)]
    diabetes_meds = [m for m in meds_at_encounter
                     if any(k in m["medication"].lower() for k in DIABETES_MED_KEYWORDS)]
    insulin_versions = [m for m in medications if "insulin" in m["medication"].lower()]
    insulin = "No"
    if insulin_versions:
        authored = parse_timestamp(insulin_versions[0]["authored_on"])
        insulin = "Up" if start <= authored <= end else ("Steady" if authored < start else "No")
    changed_during = any(start <= effective(v) <= end for m in medications for v in m["history"])
    active_conditions = [c for c in conditions if c["onset"] and parse_timestamp(c["onset"]) <= end]
    ranked = sorted(active_conditions, key=lambda c: (c["encounter"] != f"Encounter/{target['id']}", c["onset"]))
    diag_codes = [icd10_to_icd9_proxy(c["code"]) for c in ranked[:3]]
    while len(diag_codes) < 3:
        diag_codes.append("V00")
    prior_inpatient = [e for e in prior if e["class"] == "IMP"]
    prior_readmissions = 0
    for previous, current in zip(prior_inpatient, prior_inpatient[1:]):
        if previous["end"] and (parse_timestamp(current["start"]) - parse_timestamp(previous["end"])).days <= 30:
            prior_readmissions += 1
    stays = [e["length_of_stay_days"] for e in prior_inpatient if e["length_of_stay_days"] is not None]
    age_years = (start.date() - parse_timestamp(profile["birth_date"]).date()).days // 365
    gender = {"male": "Male", "female": "Female"}.get(str(profile["gender"]), "Unknown/Invalid")
    features = {
        "patient_id": patient_id,
        "age": _age_bracket(age_years),
        "gender": gender,
        "race": "Unknown",
        "time_in_hospital": max(1, min(int(target["length_of_stay_days"] or 1), 14)),
        "num_lab_procedures": len(labs),
        "num_procedures": 0,
        "num_medications": len(meds_at_encounter),
        "number_outpatient": sum(1 for e in prior_year if e["class"] == "AMB"),
        "number_emergency": sum(1 for e in prior_year if e["class"] == "EMER"),
        "number_inpatient": sum(1 for e in prior_year if e["class"] == "IMP"),
        "number_diagnoses": len(active_conditions),
        "diag_1": diag_codes[0],
        "diag_2": diag_codes[1],
        "diag_3": diag_codes[2],
        "A1Cresult": _a1c_category(latest_value("4548-4")),
        "max_glu_serum": _glucose_category(latest_value("2345-7")),
        "insulin": insulin,
        "change": "Ch" if changed_during else "No",
        "diabetesMed": "Yes" if diabetes_meds else "No",
        "admission_type_id": ADMISSION_TYPE.get(target["admit_source"], 3),
        "admission_source_id": ADMISSION_SOURCE.get(target["admit_source"], 1),
        "discharge_disposition_id": 1,
        "prior_encounter_count": len(prior_inpatient),
        "prior_inpatient_count": len(prior_inpatient),
        "prior_emergency_count": 0,
        "prior_readmission_count": prior_readmissions,
        "running_mean_time_in_hospital": float(sum(stays) / len(stays)) if stays else 0.0,
    }
    caveats = [
        "Out of distribution: the model was trained on UCI 1999-2008 diabetic inpatient encounters, "
        "not on these synthetic FHIR records.",
        "ICD-10 codes are mapped to broad ICD-9 categories (diabetes, circulatory, ...) by prefix.",
        "num_lab_procedures counts synthetic laboratory Observations and is far lower than typical UCI values.",
        "num_procedures is set to 0 because no Procedure resources are modelled.",
        "race is not recorded in the synthetic records and is passed as 'Unknown'.",
        "Prior-history features use encounter order within the synthetic record only.",
    ]
    return {
        "available": True,
        "patient_id": patient_id,
        "encounter_id": target["id"],
        "features": features,
        "caveats": caveats,
        "sources": [target["source"]] + [o["source"] for o in labs],
    }
