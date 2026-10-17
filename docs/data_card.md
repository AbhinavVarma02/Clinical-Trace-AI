# Data Card: UCI Diabetes 130-US Hospitals

This project uses the public UCI Diabetes 130-US Hospitals dataset for educational purposes.

## Source

UCI Machine Learning Repository: Diabetes 130-US hospitals for years 1999-2008.

## Files

- `data/raw/diabetic_data.csv`
- `data/raw/IDS_mapping.csv`

Raw and processed data files are intentionally ignored by Git.

## Target

- Positive class: `readmitted == "<30"`
- Negative class: `readmitted == ">30"` or `readmitted == "NO"`

## Required Cleaning

- Read `?` values as missing.
- Remove discharge disposition IDs `{11, 13, 14, 19, 20, 21}`.
- Split by `patient_nbr` using `GroupShuffleSplit`.
- Drop `patient_nbr` from model features after splitting.

## Risks

The dataset is historical, imbalanced, and may contain demographic or site-level biases. It is suitable for a portfolio prototype only.

---

# Data Card: Synthetic FHIR Patient Journeys (Clinical-Trace AI 2.0)

A second, entirely separate data source used by the clinical workspace and the review agent. It is **not** derived from the UCI dataset and the UCI dataset is not derived from it.

## Provenance and licensing

- Hand-designed, locally generated fixtures written for this project (`src/clinical/synthetic.py`), committed as `data/fhir/synthetic_bundle.json` and regenerable with `python -m src.clinical.synthetic`. Released under the repository's MIT licence.
- Every resource carries `meta.tag` `urn:clinical-trace:data-origin|synthetic` ("Synthetic record - not a real patient") and `meta.source` `urn:clinical-trace:synthetic-generator:v1.0.0`. Patient names are "<Given> Synthetic (synthetic record)"; ids are `synthetic_p001`–`synthetic_p004`.
- No real person, record, or identifier was used. Clinical details were written to be plausible, not drawn from any patient.

## Schema

FHIR R4 subset: Patient, Encounter, Condition (ICD-10-CM), Observation (LOINC; laboratory, vital-signs, social-history), MedicationRequest (RxNorm-style codes, versioned), DocumentReference (base64 `text/plain` notes), DiagnosticReport. 86 resources in total, including three-version warfarin and two-version metformin orders.

## Journeys

| Patient | Purpose | Notable content |
| --- | --- | --- |
| `synthetic_p001` | Longitudinal readmission journey | Type 2 diabetes, heart failure, CKD 3a; 5 encounters; 30-day readmission (19 days); HbA1c 7.2 → 9.4; eGFR 58 → 40; metformin stopped, insulin started |
| `synthetic_p002` | Contradictions and adversarial text | COPD without diabetes; smoking documented as current (3 sources) and former (1 source); a referral note containing a prompt-injection string |
| `synthetic_p003` | Incomplete documentation | Admission with no end time, no discharge summary, no medication orders, a unit-less HbA1c, and a note value (11.2) that matches no recorded observation (10.6) |
| `synthetic_p004` | Medication version history | Atrial fibrillation, warfarin dose changes 5 → 3 → 4 mg with an INR series and a bleeding admission |

## Known limitations

- Four patients; designed to exercise behaviours, not to represent a population.
- Notes are short and well structured compared with real clinical text.
- Patient ids contain an underscore, which the FHIR id grammar does not allow (kept for compatibility with the v1 synthetic-id rule).
- The adversarial text in `p002-d4` is intentional test content.
