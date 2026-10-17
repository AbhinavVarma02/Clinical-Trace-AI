# Requirements reconciliation

Reconciled 2026-10-17 against the original Clinical-Trace AI 2.0 specification, source, tests, private handoff, and commits through `f4f785c`. This is an incremental upgrade. A phase summary is not a claim that every requirement in that phase is complete.

| Original scope | Existing evidence | Remaining acceptance work / roadmap IDs |
| --- | --- | --- |
| P0 preservation, baseline, migration | v1 modules retained; 183 Python and 32 frontend tests pass at handoff; ADRs | Maintain compatibility on each change |
| P1 synthetic longitudinal data, FHIR, ingestion | `src/clinical/`, mock FHIR API, `test_fhir_data.py`; four patient journeys | Durable retrieval metadata and refresh/recovery audit (CT-P08-T03) |
| P2 tools, workflows, graph, structured answers | `src/agent/`, `test_agent_v2.py`, prior local LLM evaluations | Durable node checkpoints/restart/cancellation (CT-P02-T04), streaming (T05); bounded concurrency and shutdown (CT-P09-T03) |
| P3 hybrid RAG, citations, diagnostics | BM25 + LSA/RRF, versioned source references, cache, benchmark | Optional reranking (CT-P03-T03); neural general/biomedical comparison (CT-P13-T02) |
| P4 policy, guards, review, security | Default-deny policy, verifier, version/hash-bound reviews, safety tests | Retention controls (CT-P04-T06), enforce streaming body limits/proxy handling (CT-P04-T07), safe document extraction (CT-P13-T06), dependency audit review (CT-P11-T03) |
| P5 readmission discipline | Train-only preprocessing, patient split, validation selection, held-out metrics, SHAP, MLflow/model versioning | Calibration/threshold report (CT-P05-T01), drift (T02), subgroup/error report (T03); no model replacement implied |
| P6 evaluation, feedback, gates | Versioned dev/held-out cases, actual deterministic/LLM results, persisted feedback | Recovery/failure coverage (CT-P06-T04), human-review agreement requires real annotations; never manufacture ratings |
| P7 workspace and QA | Eight working sections, themes, charts, real backend, component tests, previous manual checks | Real-backend browser tests, desktop/tablet/mobile, focus, error/loading/empty states and screenshots (CT-P07-T02/T03); retain Streamlit |
| P8 service contracts/persistence | SQLite migrations, clinical resources, sessions, runs, reviews, feedback | Typed response schemas and contract/type checks (CT-P08-T04), evaluation and retrieval metadata (T03); optional Postgres/pgvector (T02, ADR 0003) |
| P9 observability/reliability | Request IDs, route-template logs, node/tool traces, circuit breaker, retry limits | Exportable open-standard traces (CT-P09-T02), concurrent/recovery/shutdown tests (T03), token/retry/cache/cost diagnostics (T04); privacy-aware retention |
| P10 resource optimization | Measured agent/retrieval latency; frontend bundle output | Local image/cold-start measurements (CT-P10-T02), concurrent throughput/memory/startup benchmark and documented tradeoffs (T03); no assumed gains from quantization |
| P11 containers/CI/public demo | Four CI jobs in run 39074327485 passed, independently verified via GitHub; container smoke passed there | Local Docker unavailable; The original Python scan was advisory; mandatory constrained scanning now passes (T03); reproducible dependency lock/format/type checks (T04). Public 2.0 deployment is reserved for separate validation phase and requires approval; keep HF v1 unchanged |
| P12 public docs/private continuity | `docs/`, ignored `.project-state/`, README, model/data cards, ADRs | Update evidence, screenshots, actual outcomes and final recovery handoff (CT-P12-T02) after each task; final reviewer-ready handoff only after core gates pass |
| P13 advanced extensions | No advanced extension completion claimed | Sequence baseline (T01), biomedical retrieval (T02), simulated SMART launch (T03), optional local kind (T04), load tests (T05), structured synthetic referral/discharge ingestion with provenance and uncertainty (T06) |

## Reconciliation outcome (continued implementation)

Completed since the initial reconciliation: P5 calibration/subgroup/drift; P7 real-backend desktop/tablet/mobile E2E and representative QA; P2 durable checkpoints, recovery, safe streaming and cancellation; P4 retention/body/proxy hardening; P8 provenance metadata and scoped response contracts; P9 local telemetry and bounded reliability; P10 bounded load/allocation measurements; P11 patched exact/constraint dependencies and mandatory Linux CI checks. Their original requirements remain mapped above to stable IDs; the roadmap records current evidence.

Actual local 7B inference and all four strengthened CI jobs passed at 6541d9b. Human-review agreement has a validated runner/protocol but is unmeasured without real annotations. The full definition of done still includes a separately authorized public 2.0 deployment, external validation and visible advanced extensions. Do not infer those from a local core milestone.

## Dependency and scope decisions

The unblocked sequence is browser verification → readmission analysis/drift → durable execution/reliability → contracts/observability/evidence → measured core validation. Advanced experiments depend on a stable core. PostgreSQL, reranking, quantization and Kubernetes are conditional investigations, not required infrastructure for a four-patient zero-cost demo. Record measured reasons for retaining the lightweight default. Public deployment, actual public inference, and external smoke tests remain open acceptance items for the separate deployment/validation phase. Passing deterministic tests does not revalidate earlier LLM outputs or establish clinical safety.
