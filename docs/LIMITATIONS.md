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
- The handoff container passed real build/smoke tests in Actions 39074327485. Updated dependencies/runtime passed all four jobs in Actions 39077304115 at 6541d9b; inspect newer branch-tip CI separately. Local Docker became available on 2026-10-18; image size, cold start and memory are recorded in `evaluation/results/container_resources.json` (desktop CPU throttled with `--cpus`, an approximation of hosted vCPUs).
- The public edge's limits are free-tier platform limits (100,000 Worker requests/day, an 8,000-neuron inference cap). Under heavy use, live inference pauses until 00:00 UTC and agent runs are refused after 300 per day; both are disclosed in the UI.
- The public backend is co-hosted in the free Space ([PUBLIC_DEPLOYMENT.md](PUBLIC_DEPLOYMENT.md)); it sleeps after 48 idle hours and the full wake time is unmeasured. Live inference is relayed by the edge and works only on streamed requests; other requests answer in labelled deterministic mode.
- Free hosts provide no persistent disk for this setup: public runs, reviews and audit events live in an ephemeral SQLite file and disappear on restart or sleep.
- Public workspaces are random browser-held identifiers, not accounts. Anyone with the same browser storage sees the same workspace; clearing storage starts a new one.

## Evaluation

- Single-author synthetic cases; substring-based correctness; no human or model-based judging.
- Held-out cases are separate from development cases but not independent of the author.
- Live-LLM answers lose claims in verification: in a 48-run sample 19% of drafted claims were excluded, concentrated in the multi-encounter summary and warfarin questions, and the model's draft for the guided summary was unusable in about half the runs (deterministic fallback). Many exclusions are conservative false rejections (date and unit wording, past-tense "diagnosed/prescribed" record statements). Excluded claims and reasons are shown in the UI; see [UI_POLISH.md](UI_POLISH.md#verification-badge-follow-up-2026-10-19).
