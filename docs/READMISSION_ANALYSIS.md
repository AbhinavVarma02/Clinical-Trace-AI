# Readmission calibration and subgroup audit

Measured 2026-10-17 using the preserved XGBoost model `20260703051249-local`. This audit fits no estimator and changes no deployed artifacts, probabilities, thresholds, or synthetic FHIR mappings.

Run `CLINICAL_TRACE_SKIP_DOTENV=1 python -m evaluation.readmission` with the original local UCI CSV, model/pipeline, metadata and processed matrices. The runner verifies the raw SHA-256, reconstructs the seed-42 patient-grouped partitions, checks every transformed matrix/label and patient count, checks zero patient overlap, and reproduces saved test metrics before writing aggregate results. It never downloads data. Missing/mismatched provenance fails closed. Result: [readmission_report.json](../evaluation/results/readmission_report.json), with file hashes and package versions. Source commit identifies the base revision; the implementing commit is available in Git history.

| Measure | Held-out result |
| --- | --- |
| Encounters / patients | 19,802 / 13,998 |
| Decision threshold | 0.30; independently reproduced using validation only |
| ROC-AUC / average precision | 0.682674 / 0.239759 |
| Recall / precision | 0.667870 / 0.169744 |
| Brier score / log loss | 0.133610 / 0.437971 |
| Equal-width 10-bin ECE | 0.190108 |
| Mean score / observed positive rate | 0.302015 / 0.111908 |
| False positives / false negatives | 7,239 / 736 |

The scores substantially overestimate observed readmission frequency. Class-weighted training optimizes discrimination under imbalance and does not guarantee calibrated probabilities. Treat these outputs as model scores with limitations. No recalibration was selected on test data or applied to the application. A future calibrator would need its own patient-separated calibration/selection protocol and an untouched evaluation partition; this audit does not establish that it improves the model.

The result artifact includes all reliability bins, validation threshold candidates and age/race/gender subgroup confusion matrices, Brier/ECE and ranking metrics. Metrics are suppressed below 100 encounters or when only one outcome class is present. The `[0-10)` age group has 29 encounters and is suppressed. Suppression and missing values remain explicit rather than appearing as zero performance.

For example, female/male recall is 0.694/0.638 and ECE is 0.197/0.182 at the same frozen threshold. These are descriptive differences, not causal evidence or a fairness certification. Subgroup event counts vary; patient encounters are correlated. No independence, confidence-interval or statistical-significance claims are made. Missing race is reported as its own group. Small minority groups remain uncertain even above the minimum count.

The false-positive and false-negative totals describe the current operational tradeoff. The threshold sweep reports validation results only; all test and subgroup metrics use the existing deployed threshold. The historical 1999–2008 UCI benchmark and locally generated synthetic clinical journeys remain separate datasets. None of these results establishes clinical suitability.
