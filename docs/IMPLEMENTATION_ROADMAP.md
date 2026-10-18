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
| P2 | CT-P02-T04 | LangGraph checkpoint persistence beyond memory | TESTED | `src/agent/checkpoints.py`, restart/replay tests |
| P2 | CT-P02-T05 | Streaming responses | TESTED | SSE progress, verified final response, cancellation; `tests/test_agent_stream.py` and browser suite |
| P3 | CT-P03-T01 | Chunking, BM25, LSA dense, RRF, filters, cache, versioning | TESTED | `src/retrieval/`, `tests/test_retrieval_v2.py` |
| P3 | CT-P03-T02 | Retrieval benchmark and isolation probes | VERIFIED | `docs/EVALUATION.md` |
| P3 | CT-P03-T03 | Reranking | NOT_STARTED | — |
| P4 | CT-P04-T01 | Default-deny policy, personas, audit | TESTED | `src/safety/policy.py`, `tests/test_safety_v2.py` |
| P4 | CT-P04-T02 | Input guards, injection flagging, output rules | TESTED | `src/safety/guards.py` |
| P4 | CT-P04-T03 | Claim-level evidence verification | TESTED | `src/safety/verifier.py` |
| P4 | CT-P04-T04 | Hash-bound human review workflow | VERIFIED | `src/governance/review.py`, `tests/test_api_v2.py`, browser check |
| P4 | CT-P04-T05 | Rate/size limits, structured errors, container hardening | VERIFIED | HTTP tests and actual non-root container smoke in CI 39077304115 |
| P5 | CT-P05-T01 | Calibration analysis, threshold report | VERIFIED | `docs/READMISSION_ANALYSIS.md`, aggregate artifact |
| P5 | CT-P05-T02 | Data/prediction drift monitoring | VERIFIED | VERIFIED on historical splits; `docs/DRIFT_MONITORING.md`, tests and aggregate artifact; no live/temporal claim |
| P5 | CT-P05-T03 | Subgroup evaluation and error analysis | VERIFIED | `evaluation/readmission.py`, `tests/test_readmission_analysis.py` |
| P6 | CT-P06-T01 | Versioned dev/held-out datasets, thresholds, runner | VERIFIED | `evaluation/` |
| P6 | CT-P06-T02 | CI regression gate | TESTED | `tests/test_evaluation_gate.py` |
| P6 | CT-P06-T03 | Feedback capture (no automatic learning) | TESTED | `POST /agent/runs/{id}/feedback` |
| P7 | CT-P07-T01 | React workspace: 8 sections, light/dark, responsive | VERIFIED | `frontend/workspace/`, Vitest, browser checks |
| P7 | CT-P07-T02 | Playwright end-to-end tests | VERIFIED | 24 Chromium tests across desktop/tablet/mobile; Linux CI 39077304115 passed |
| P8 | CT-P08-T01 | v2 API, error contract, migrations, persistence | TESTED | `api/v2.py`, `src/persistence/db.py` |
| P8 | CT-P08-T02 | PostgreSQL + pgvector backend | NOT_STARTED | ADR 0003 |
| P9 | CT-P09-T01 | Request ids, request log, identifier-only traces, ops metrics | TESTED | `api/middleware.py`, `/api/v2/ops/metrics` |
| P9 | CT-P09-T02 | OpenTelemetry exporter | TESTED | Local-only SDK exporter; trace correlation/content exclusion tests |
| P10 | CT-P10-T01 | Latency measured per mode | VERIFIED | `docs/EVALUATION.md` |
| P10 | CT-P10-T02 | Image size / cold start measurement | VERIFIED | Local Docker 2026-10-18: 2.01 GB image; ready 16.6 s at 2 vCPU, 248.8 s at 0.1 vCPU/512 MB; peak ~296 MiB; `evaluation/results/container_resources.json` |
| P11 | CT-P11-T01 | Multi-stage non-root Dockerfile, CI workflow | VERIFIED | VERIFIED at 6541d9b; Actions 39077304115: all four jobs passed including real E2E, audit and container smoke |
| P11 | CT-P11-T02 | Separate 2.0 public deployment and external validation | VERIFIED | Public release gate passed 2026-10-18 on desktop/mobile incl. cold start (`docs/PUBLIC_DEPLOYMENT.md`) |
| P12 | CT-P12-T01 | Architecture, API, safety, evaluation, deployment, limitations docs | VERIFIED | `docs/` |
| P13 | CT-P13-T01 | Sequence model vs tabular baseline | NOT_STARTED | — |
| P13 | CT-P13-T02 | Neural general vs biomedical embedding comparison | NOT_STARTED | needs model downloads (free) and benchmark time |
| P13 | CT-P13-T03 | Simulated SMART on FHIR launch | NOT_STARTED | — |
| P13 | CT-P13-T04 | Local Kubernetes (kind) manifests | NOT_STARTED | — |
| P13 | CT-P13-T05 | Concurrency / load tests | VERIFIED | VERIFIED bounded local workload; deeper stress pending; 40 requests at 1/4/8 concurrency; no native-memory/replica/large-corpus claim |

## Reconciled requirements

See [REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md). The following original requirements were missing from the previous task table; they remain visible without duplicating implemented capabilities.

| Phase | ID | Task | Status | Evidence / dependency |
| --- | --- | --- | --- | --- |
| P4 | CT-P04-T06 | Configurable retention of sessions, checkpoints, traces, audit and feedback | TESTED | `tests/test_retention.py`, explicit CLI with holds |
| P4 | CT-P04-T07 | Streaming request-size enforcement and trusted-proxy rate-limit identity | TESTED | Actual bytes, body timeout, peer-based bucket, memory bound; HTTP tests |
| P6 | CT-P06-T04 | Recovery/failure evaluations and human-review agreement protocol | TESTED | TESTED; human agreement unmeasured; Recovery/provider-outage tests; paired annotation validator; real annotations required |
| P7 | CT-P07-T03 | Tablet, loading/empty/error and keyboard QA, representative screenshots | TESTED | TESTED; representative views inspected; Real browser states, focus, stream tests and `docs/images`; broader manual release QA remains in validation phase |
| P8 | CT-P08-T03 | Persist retrieval/index and evaluation metadata with reproducible migrations | TESTED | Migration6, metadata-contract tests; resources/runs/reviews unchanged |
| P8 | CT-P08-T04 | Typed response contracts, API contract validation, Python type checks | TESTED | TESTED (scoped); Agent/retrieval/status response schemas, contract tests; mypy covers four new modules |
| P9 | CT-P09-T03 | Controlled concurrency, restart recovery, cancellation ownership, graceful shutdown | TESTED | `tests/test_agent_recovery.py`, `docs/RELIABILITY.md` |
| P9 | CT-P09-T04 | Token/retry/cache/cost diagnostic completeness | TESTED | Provider retry/token/circuit/local-cost counters; cache stats retained; hosted costs unknown |
| P10 | CT-P10-T03 | Reproducible throughput, memory, startup and bundle measurements | VERIFIED | VERIFIED locally (partial memory scope); `evaluation/results/performance_local.json`; excludes native RSS/container startup |
| P11 | CT-P11-T03 | Review dependency audit findings and explicit security gate policy | VERIFIED | VERIFIED locally; Patched constrained runtime: 144 packages / no known findings; mandatory CI audit |
| P11 | CT-P11-T04 | Reproducible Python constraints, formatter/type/contract CI coverage | VERIFIED | VERIFIED at 6541d9b; Exact roots/transitive snapshot; Linux CI scoped formatter/mypy/security gates passed |
| P12 | CT-P12-T02 | Core evidence reconciliation and final reviewer-ready recovery handoff | VERIFIED | docs/VALIDATION_HANDOFF.md; local/CI/LLM evidence and visible remaining release gates |
| P13 | CT-P13-T06 | Structured referral/discharge ingestion, provenance, extraction uncertainty | NOT_STARTED | P4 safe parsing; stable core |
| P6 | CT-P06-T05 | Independent paired human-review assessment | BLOCKED | No real reviewer annotations; protocol/runner implemented, no agreement claim |
| P13 | CT-P13-T07 | Extended native-memory, long-duration, replica/large-corpus stress experiments | NOT_STARTED | Bounded local workload already measured; extension beyond core acceptance |

| P11 | CT-P11-T05 | Zero-cost public edge: static workspace, guarded gateway, rate limits, daily caps, D1 accounting | VERIFIED | `deploy/cloudflare`, 19 Worker tests, live smoke, release script with auto-rollback, rollback drill |
| P11 | CT-P11-T06 | Free hosted inference selection and benchmark | VERIFIED | Workers AI Qwen3-30B-A3B; recorded gate pass 36/42; model comparison in `docs/EVALUATION.md`; quota exhaustion handling tested |
| P11 | CT-P11-T07 | Public-mode visitor isolation and edge-only backend | TESTED | `tests/test_public_mode.py`; co-host verify; full-chain smoke |
| P11 | CT-P11-T08 | Cold-start recovery and availability UX; guided 90-second case | VERIFIED | Playwright cold-start and guided-case tests (3 viewports); full-chain browser run |
| P11 | CT-P11-T09 | Public backend host (independent free host or approved co-hosting) | VERIFIED | Owner-approved co-hosting in the existing Space; inference relayed through the edge (`src/agent/relay.py`); rollback verified |

## Next phase and remaining dependencies

1. Done 2026-10-18: public backend co-hosted, release gate passed through the public URL. Next: measure the 48-hour sleep wake time; owner decision on merging to `main`.
2. Done 2026-10-18: local Docker measurements (CT-P10-T02).
3. Done 2026-10-18: free hosted inference selected and benchmarked (CT-P11-T06).
4. Collect independent human annotations only with real reviewers; use the paired rubric/runner without fabricating agreement.
5. Advanced extensions remain dependency-gated and conditional: no timestamp invention for sequence models; predeclare a larger independent retrieval benchmark before embedding/reranking selection; keep SMART launch simulated and kind optional; implement structured document extraction with provenance/uncertainty. PostgreSQL/pgvector remains optional under ADR 0003 for the current 117-chunk corpus.
6. Preserve HF v1, default branch, private files and current model artifacts throughout. No merge, paid service or public overwrite is authorized.
