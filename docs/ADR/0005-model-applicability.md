# ADR 0005: The readmission model is applied only where the record supports it

**Status:** accepted (2026-10-17)

**Context.** The XGBoost model was trained on UCI diabetic inpatient encounters (ICD-9, 1999–2008). The synthetic FHIR patients use ICD-10, have far fewer labs, and include non-diabetic and incomplete records.

**Decision.** `src/clinical/risk_bridge.py` requires a diabetes diagnosis, a completed inpatient encounter with a discharge time, and a medication list. Eligible encounters are mapped with listed approximations; all others return `available: false` with reasons. Every prediction carries out-of-distribution caveats.

**Consequences.** Two of four synthetic patients receive predictions; the others show why not. No synthetic record is presented as UCI data, and benchmark metrics are never presented as performance on synthetic records.
