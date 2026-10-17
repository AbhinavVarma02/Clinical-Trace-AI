# Limitations

## Clinical scope

- Educational prototype on synthetic data. Not a medical device, not clinically validated, not evaluated with clinicians.
- Outputs restate what the synthetic record documents; they are not clinical judgements.

## Data

- Four hand-designed synthetic patients (86 FHIR resources). They exercise specific behaviours (readmission, contradiction, incompleteness, versioned orders) and are not a representative population.
- FHIR support covers a validated subset of seven resource types; it is not a full R4 implementation. Patient ids use an underscore (`synthetic_p001`), which FHIR's id grammar does not allow; kept for compatibility with the v1 synthetic-id rule.
- The mock FHIR server and HTTP client have no SMART on FHIR or OAuth; no real EHR has been connected.

## Readmission model

- Trained on UCI Diabetes 130-US (1999–2008). Applying it to synthetic FHIR patients requires approximations (ICD-10 → broad ICD-9 categories, synthetic lab counts far below UCI norms, no procedures, unknown race). Outputs are out of distribution and uncalibrated for this population.
- Calibration/subgroup/error and historical distribution audits are implemented; scores substantially overestimate observed UCI event frequency (ECE 0.1901). No recalibration or retraining was applied. Sequence modeling remains an advanced extension.

## Agent

- Deterministic mode answers only intents covered by its rules; other questions use extractive quotes or abstain.
- LLM mode quality depends heavily on model size: `qwen2.5:1.5b` failed the quality gate; `qwen2.5:7b` passed but is ~180× slower than deterministic mode locally.
- Guards are pattern-based and can be evaded by unanticipated phrasing.
- Verification checks citations, numbers, computations, and lexical overlap. It does not prove semantic entailment: a claim that reuses the evidence's words with a different meaning could pass.
- Contradiction detection covers smoking status and lab values quoted in notes only.
- Actual progress is streamed; clinical answer text is returned only after verification. Durable SQLite checkpoints support explicit compatible restart recovery. Cancellation is cooperative between nodes, not a forced interruption of running Python code.

## Retrieval

- Dense retrieval uses LSA (TF-IDF + SVD), not a neural encoder. No biomedical-embedding comparison has been run.
- Hybrid fusion lowered precision@5 relative to BM25 on the benchmark (see EVALUATION.md).
- Benchmark size is 19 queries.

## Platform

- SQLite only; PostgreSQL and pgvector are not implemented.
- Demo personas are not authentication.
- Rate limits and caches are in-process and reset on restart; they do not coordinate across replicas.
- OpenTelemetry export is opt-in and local-file only; no network collector is configured. Operators must rotate trace files separately from SQLite retention.
- The handoff container passed real build/smoke tests in Actions 39074327485. Updated dependencies/runtime passed all four jobs in Actions 39077304115 at 6541d9b; inspect newer branch-tip CI separately. Local Docker remains unavailable through WSL integration.

## Evaluation

- Single-author synthetic cases; substring-based correctness; no human or model-based judging.
- Held-out cases are separate from development cases but not independent of the author.
