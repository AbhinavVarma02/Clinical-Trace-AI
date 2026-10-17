# Implementation Roadmap

Status values: `NOT_STARTED`, `IN_PROGRESS`, `IMPLEMENTED` (code exists), `TESTED` (automated tests pass), `VERIFIED` (tests plus manual/browser or measured evidence), `BLOCKED`.
Evidence references point to files, tests, or result artifacts. Branch: `feature/clinical-trace-2.0`.

| Phase | ID | Task | Status | Evidence |
| --- | --- | --- | --- | --- |
| P0 | CT-P00-T01 | Baseline audit, tests (54 Python, 24 frontend passing) | VERIFIED | `docs/PROJECT_STATUS.md` |
| P1 | CT-P01-T01 | Synthetic FHIR R4 journeys (4 patients, 86 resources) | TESTED | `src/clinical/synthetic.py`, `tests/test_fhir_data.py` |
| P1 | CT-P01-T02 | Validation, versioned idempotent ingestion, lineage, timestamp normalisation | TESTED | `src/clinical/store.py`, `fhir_models.py` |
| P1 | CT-P01-T03 | Mock FHIR server + HTTP client (paging, retries, scope checks) | TESTED | `api/fhir.py`, `src/clinical/fhir_client.py` |
| P1 | CT-P01-T04 | Model-applicability bridge with explicit unavailability | TESTED | `src/clinical/risk_bridge.py` |
| P2 | CT-P02-T01 | Typed patient-bound tools (11) | TESTED | `src/agent/tools.py`, `tests/test_agent_v2.py` |
| P2 | CT-P02-T02 | LangGraph orchestration, budgets, cancellation, sessions | TESTED | `src/agent/graph.py`, `service.py` |
| P2 | CT-P02-T03 | Real LLM inference via OpenAI-compatible provider (Ollama measured) | VERIFIED | `evaluation/results/llm_*.json` |
| P2 | CT-P02-T04 | LangGraph checkpoint persistence beyond memory | NOT_STARTED | — |
| P2 | CT-P02-T05 | Streaming responses | NOT_STARTED | — |
| P3 | CT-P03-T01 | Chunking, BM25, LSA dense, RRF, filters, cache, versioning | TESTED | `src/retrieval/`, `tests/test_retrieval_v2.py` |
| P3 | CT-P03-T02 | Retrieval benchmark and isolation probes | VERIFIED | `docs/EVALUATION.md` |
| P3 | CT-P03-T03 | Reranking | NOT_STARTED | — |
| P4 | CT-P04-T01 | Default-deny policy, personas, audit | TESTED | `src/safety/policy.py`, `tests/test_safety_v2.py` |
| P4 | CT-P04-T02 | Input guards, injection flagging, output rules | TESTED | `src/safety/guards.py` |
| P4 | CT-P04-T03 | Claim-level evidence verification | TESTED | `src/safety/verifier.py` |
| P4 | CT-P04-T04 | Hash-bound human review workflow | VERIFIED | `src/governance/review.py`, `tests/test_api_v2.py`, browser check |
| P4 | CT-P04-T05 | Rate/size limits, structured errors, container hardening | TESTED (container: IMPLEMENTED) | `api/middleware.py`, `Dockerfile` |
| P5 | CT-P05-T01 | Calibration analysis, threshold report | VERIFIED | `docs/READMISSION_ANALYSIS.md`, aggregate artifact |
| P5 | CT-P05-T02 | Data/prediction drift monitoring (`monitoring/drift_report.py` is a placeholder) | NOT_STARTED | — |
| P5 | CT-P05-T03 | Subgroup evaluation and error analysis | VERIFIED | `evaluation/readmission.py`, `tests/test_readmission_analysis.py` |
| P6 | CT-P06-T01 | Versioned dev/held-out datasets, thresholds, runner | VERIFIED | `evaluation/` |
| P6 | CT-P06-T02 | CI regression gate | TESTED | `tests/test_evaluation_gate.py` |
| P6 | CT-P06-T03 | Feedback capture (no automatic learning) | TESTED | `POST /agent/runs/{id}/feedback` |
| P7 | CT-P07-T01 | React workspace: 8 sections, light/dark, responsive | VERIFIED | `frontend/workspace/`, Vitest, browser checks |
| P7 | CT-P07-T02 | Playwright end-to-end tests | VERIFIED locally | 24 Chromium tests across desktop/tablet/mobile; CI step pending remote execution |
| P8 | CT-P08-T01 | v2 API, error contract, migrations, persistence | TESTED | `api/v2.py`, `src/persistence/db.py` |
| P8 | CT-P08-T02 | PostgreSQL + pgvector backend | NOT_STARTED | ADR 0003 |
| P9 | CT-P09-T01 | Request ids, request log, identifier-only traces, ops metrics | TESTED | `api/middleware.py`, `/api/v2/ops/metrics` |
| P9 | CT-P09-T02 | OpenTelemetry exporter | NOT_STARTED | — |
| P10 | CT-P10-T01 | Latency measured per mode | VERIFIED | `docs/EVALUATION.md` |
| P10 | CT-P10-T02 | Image size / cold start measurement | BLOCKED | Docker daemon not running locally |
| P11 | CT-P11-T01 | Multi-stage non-root Dockerfile, CI workflow | VERIFIED at f4f785c | Actions 39074327485: all four jobs passed; subsequent E2E additions pending CI |
| P11 | CT-P11-T02 | Hugging Face Space update to 2.0 | BLOCKED | Owner approval required |
| P12 | CT-P12-T01 | Architecture, API, safety, evaluation, deployment, limitations docs | IMPLEMENTED | `docs/` |
| P13 | CT-P13-T01 | Sequence model vs tabular baseline | NOT_STARTED | — |
| P13 | CT-P13-T02 | Neural general vs biomedical embedding comparison | NOT_STARTED | needs model downloads (free) and benchmark time |
| P13 | CT-P13-T03 | Simulated SMART on FHIR launch | NOT_STARTED | — |
| P13 | CT-P13-T04 | Local Kubernetes (kind) manifests | NOT_STARTED | — |
| P13 | CT-P13-T05 | Concurrency / load tests | NOT_STARTED | — |

## Reconciled requirements

See [REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md). The following original requirements were missing from the previous task table; they remain visible without duplicating implemented capabilities.

| Phase | ID | Task | Status | Evidence / dependency |
| --- | --- | --- | --- | --- |
| P4 | CT-P04-T06 | Configurable retention of sessions, checkpoints, traces, audit and feedback | NOT_STARTED | P8 persistence |
| P4 | CT-P04-T07 | Streaming request-size enforcement and trusted-proxy rate-limit identity | NOT_STARTED | `api/middleware.py` currently trusts client forwarded headers |
| P6 | CT-P06-T04 | Recovery/failure evaluations and human-review agreement protocol | NOT_STARTED | Actual reviewer annotations required for agreement claims |
| P7 | CT-P07-T03 | Tablet, loading/empty/error and keyboard QA, representative screenshots | NOT_STARTED | P7-T02 |
| P8 | CT-P08-T03 | Persist retrieval/index and evaluation metadata with reproducible migrations | NOT_STARTED | Existing resources/runs/feedback/reviews already durable |
| P8 | CT-P08-T04 | Typed response contracts, API contract validation, Python type checks | NOT_STARTED | Existing request validation retained |
| P9 | CT-P09-T03 | Controlled concurrency, restart recovery, cancellation ownership, graceful shutdown | NOT_STARTED | P2-T04 |
| P9 | CT-P09-T04 | Token/retry/cache/cost diagnostic completeness | NOT_STARTED | No paid provider activation; unknown costs remain unknown |
| P10 | CT-P10-T03 | Reproducible throughput, memory, startup and bundle measurements | NOT_STARTED | P9-T03 |
| P11 | CT-P11-T03 | Review dependency audit findings and explicit security gate policy | NOT_STARTED | Existing pip-audit is advisory |
| P11 | CT-P11-T04 | Reproducible Python lock, formatter/type/contract CI coverage | NOT_STARTED | Preserve compatible runtime set |
| P12 | CT-P12-T02 | Core evidence reconciliation and final reviewer-ready recovery handoff | IN_PROGRESS | Do not declare completion with open core gates |
| P13 | CT-P13-T06 | Structured referral/discharge ingestion, provenance, extraction uncertainty | NOT_STARTED | P4 safe parsing; stable core |

## Next executable tasks (in order)

1. CI run 39074327485: all four jobs passed, independently verified 2026-10-17. Preserve this historical evidence.
2. Start Docker locally, build the image, and record size and cold-start time (CT-P10-T02).
3. Playwright real-backend smoke tests for guided demos (CT-P07-T02, IN_PROGRESS).
4. Readmission model calibration and subgroup report (P5).
5. Neural embedding comparison (CT-P13-T02).
6. Hugging Face Space update after approval (CT-P11-T02).
