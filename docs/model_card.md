# Model Card: Clinical-Trace AI

This is an educational, privacy-aware prototype using public and/or synthetic healthcare data. It is designed to demonstrate healthcare AI observability, not to provide medical advice or replace clinical judgment.

## Model Details

- Task: Predict 30-day readmission risk for diabetes hospital encounters.
- Model candidates: Logistic Regression, Random Forest, XGBoost (small hyperparameter search).
- Selection metric: Highest validation F1 among thresholds with validation recall >= 0.65 (falls back to validation F2 if none qualify).
- Split: Patient-grouped (`patient_nbr`) train / validation / test via `GroupShuffleSplit`. Threshold and model chosen on validation; metrics reported on held-out test.
- Deployed model: XGBoost. Decision threshold ~0.30 (tuned on validation).
- Artifacts: `models/best_model.joblib`, `models/preprocessing_pipeline.joblib`, and `models/model_metadata.json`.

## Intended Use

This model is intended for portfolio demonstration of healthcare ML preprocessing, model training, explainability, API deployment, and observability patterns.

## Out-Of-Scope Use

Do not use this model for diagnosis, treatment decisions, medication decisions, real patient triage, operational allocation, or any clinical workflow.

## Training Data

The MVP uses the UCI Diabetes 130-US Hospitals dataset for years 1999-2008. The target maps `readmitted == "<30"` to the positive class and all other readmission labels to the negative class. Encounters ending in death or hospice discharge are removed because those patients cannot be readmitted.

## Performance

Training writes deployed metrics to `models/model_metadata.json`. Because the positive class is roughly 11%, positive-class recall, precision, F1, PR-AUC, and balanced accuracy should be read before accuracy.

Approximate held-out **test** performance for the current deployed model (exact values vary slightly per run):

| Metric | Value |
| --- | --- |
| Accuracy | ~0.597 |
| Balanced accuracy | ~0.628 |
| Recall (positive) | ~0.668 |
| Precision (positive) | ~0.170 |
| Specificity | ~0.588 |
| F1 (positive) | ~0.271 |
| F2 (positive) | ~0.421 |
| ROC-AUC | ~0.683 |
| PR-AUC (avg. precision) | ~0.240 |

These numbers are honest and deliberately not inflated. Ranking metrics (ROC-AUC / PR-AUC) sit near the documented ceiling (~0.65–0.68 ROC-AUC) for 30-day readmission on this dataset with these features. The current model adds leakage-safe longitudinal patient-history features (prior encounter / inpatient / emergency / readmission counts, running mean length of stay, first-encounter flag), computed from strictly earlier encounters ordered by `encounter_id` (a within-patient sequence proxy — no date columns exist, so no time-gap features are built). These produced the largest single ranking gain to date (ROC-AUC ~0.675 → ~0.683, PR-AUC ~0.236 → ~0.240); the validation-tuned threshold traded ~1.7 points of recall for higher specificity, leaving F1 flat. Earlier safe feature engineering (missingness indicators, train-only Top-N grouping of `payer_code`/`medical_specialty`, lab "measured" flags, comorbidity count), the leakage-free train/validation/test protocol (threshold tuned on validation, not test), and PR-AUC / balanced accuracy / specificity reporting remain in place. The recall floor of 0.65 intentionally caps achievable precision and F1.

### Leakage safety of longitudinal features

- Features use only encounters strictly earlier than the current row (per-patient `shift(1)`); a row's own values and its own `readmitted` label are never included in its `prior_*` counts.
- Because the split is by `patient_nbr`, all of a patient's encounters stay in the same partition, so within-patient history cannot cross the train/validation/test boundary.
- `encounter_id` is used only to order encounters within a patient and is never itself a model feature. `patient_nbr` is never a feature.
- Single-record inference has no history and safely defaults to first-encounter values (`is_first_encounter = 1`, all prior counts 0).
- Fidelity caveat: "prior" counts cover only this dataset's diabetic encounters (1999–2008) after deceased/hospice filtering, so they are lower bounds on true patient history.

## Limitations

- Not clinically validated.
- Historical 1999-2008 data may not reflect current practice.
- Class imbalance is substantial.
- Multiple encounters per patient require careful grouped splitting.
- Dataset collection and coding practices may embed demographic and institutional bias.

## Ethical Considerations

Outputs must be treated as model behavior demonstrations, not clinical evidence. Explanations must attribute observations to the model's analysis and include: "This is decision-support only and not medical advice."

## Use inside the Clinical-Trace AI 2.0 workspace

The workspace can show this model's output for a synthetic FHIR patient only when `src/clinical/risk_bridge.py` can map the record to the model's inputs (diabetes diagnosis, completed inpatient encounter with a discharge time, recorded medications). Two of the four synthetic patients qualify. Every such output is labelled out of distribution and lists the approximations: ICD-10 codes mapped to broad ICD-9 categories, laboratory counts derived from far fewer synthetic observations than UCI encounters contain, procedures set to zero, race unknown, and prior history limited to the synthetic record. The benchmark metrics above describe UCI held-out performance, not performance on synthetic records, and calibration on that population is unknown. Ineligible records return "model unavailable" with reasons rather than a prediction.
