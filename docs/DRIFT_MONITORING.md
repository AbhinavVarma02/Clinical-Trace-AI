# Data and prediction distribution diagnostics

`monitoring/drift_report.py` provides reference-only numerical quantile bins and categorical top-20 vocabularies, explicit missing/other/unseen bins, aggregate PSI and unseen/missing rates. Constant numerical reference values are isolated so shifts in either direction are detected. Reports contain allowed structured model features (and original UCI age brackets), never identifiers or free-text records. Prediction arrays must be aligned finite scores in [0,1]. Empty/unsupported inputs fail clearly.

```bash
python -m monitoring.drift_report --reference reference.csv --current current.csv --columns age race gender num_medications --prediction-column risk_score
python -m evaluation.readmission_drift
```

The generic CLI writes an ignored local report by default. The reproducible benchmark runner verifies the preserved raw hash, grouped splits, matrices and labels, uses the existing model without fitting, and writes [readmission_drift.json](../evaluation/results/readmission_drift.json). It compares patient-disjoint **training versus test**, not different time periods or a real deployment stream. Prediction PSI measured 0.000411358 and none of the nine selected feature comparisons exceeded the preset 0.2 alert threshold. This is evidence of similarity under this binning, not a universal no-drift result or evidence of current clinical representativeness. It does not repair the model's documented calibration gap.

PSI uses 1e-6 additive count smoothing with renormalization. The 0.2 alert threshold is a documented operational heuristic, not a p-value or clinical cutoff. Binning and sample sizes affect it. Without outcomes this cannot establish concept or performance drift. Operators must record window provenance and review alerts; nothing automatically retrains, recalibrates, changes thresholds or deploys. No monitoring of real patient data is enabled.

`tests/test_drift.py` verifies identical/shifted distributions, missingness, reference-only vocabulary/bin fitting, constant features and rejection of identifier columns or invalid prediction windows.
