# Project Status

**Updated:** 2026-10-17 · **Version:** 2.0.0-dev · **Branch:** `feature/clinical-trace-2.0` · **Public demo:** v1 unchanged. No merge or 2.0 deployment.

The existing project was continued from `f4f785c`. The local core implementation milestone is implemented and tested; the full master plan is **not a completed public release**. The separate reviewer-ready deployment/validation phase and conditional advanced extensions remain visible in the [roadmap](IMPLEMENTATION_ROADMAP.md). See [VALIDATION_HANDOFF.md](VALIDATION_HANDOFF.md) for the release boundary.

| Area | Verified evidence |
| --- | --- |
| v1 preprocessing, three model families, patient splits, SHAP, MLflow, API and Streamlit | Original functionality retained; original model/processed artifacts unchanged; compatibility tests pass |
| Synthetic FHIR, hybrid retrieval, citations, review and policy | Four synthetic journeys; versioned sources; API/integration/safety tests |
| Agent persistence/reliability | SQLite node checkpoints, explicit restart/resume, owner-only cancellation, idempotency, bounded capacity, retained review evidence and cooperative shutdown |
| Streaming/UI isolation | Real progress then verified final answer; stream interruption handling, cancellation, stale-persona response protection and accessible evidence drawers |
| Readmission audit | Existing held-out metrics reproduced; Brier 0.133610, ECE 0.190108; subgroup/errors/validation sweep; no recalibration or threshold change |
| Drift diagnostics | Reference-only bins/vocabulary; actual train/test prediction PSI 0.000411358; not temporal/live drift |
| Metadata/observability | Index/evaluation metadata persisted; typed agent/retrieval/status responses; optional local OpenTelemetry traces exclude source text |
| Dependencies/CI | Exact roots and transitive constraints; final local runtime audit: 144 packages, no known findings; mandatory audit/type/format/browser gates |
| Local performance | 40 requests each at concurrency 1/4/8; at four: p95 248.9 ms, 40/40 answered; at eight: 28/40 capacity rejections, 12 answered; zero observed isolation violations |

## Reproducible checks

| Check | Result / scope |
| --- | --- |
| Python suite | **216 passed**, including compatibility, recovery, streaming, metadata, HTTP limits and annotation validation |
| Frontend `npm run check` | **36 tests**, TypeScript and both builds passed |
| Playwright | **24 real-backend Chromium cases**, desktop/tablet/mobile; source versions, SSE, review approval, safety, persona/session isolation, themes and focus |
| Ruff / format / mypy | Lint passed; formatting scoped to new modules; mypy explicitly covers four new contract/persistence/telemetry modules |
| Deterministic evaluation | Gate PASS; 41/42 cases; `ho-013` lexical miss retained; no known test isolation/authorization violations |
| Actual local 7B inference | At `6541d9b`: gate PASS, 35/42, correctness 0.7391, rejected draft-claim rate 0.2308, p50 2.673 s/p95 8.018 s; historical reports retained |
| Linux CI/container | [Actions 39077304115](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/39077304115) at `6541d9b`: all four jobs passed, including real browsers, mandatory security audit and actual container build/smoke |

The latest branch run is available in [GitHub Actions](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions?query=branch%3Afeature%2Fclinical-trace-2.0). Keep commit-specific evidence separate from subsequent changes. The earlier run 39074327485 also passed, but its advisory security step contained findings; those were inspected and fixed, not treated as a clean audit.

## Remaining work and constraints

- Local Docker image-size/cold-start/native-memory measurements require Docker Desktop WSL integration, currently unavailable. CI container verification is real but is not a local benchmark.
- Keep the existing Hugging Face v1 demo unchanged. A separately authorized free 2.0 deployment needs infrastructure/credential decisions and unauthenticated external validation.
- Public free-CPU inference suitability remains unmeasured; local 7B results do not establish hosting responsiveness. No paid provider was enabled.
- Human-review agreement has a tested protocol/runner, but no actual annotations or clinical validation study. No score is fabricated.
- Conditional advanced work remains: sequence-model research, biomedical/general embedding comparison, optional reranking/PostgreSQL, simulated SMART launch, kind/Kubernetes, deeper memory/stress tests and structured referral/discharge extraction. None is silently marked complete.

Reproduction and evidence: [requirements reconciliation](REQUIREMENTS_TRACEABILITY.md), [reliability](RELIABILITY.md), [readmission audit](READMISSION_ANALYSIS.md), [dependency security](DEPENDENCY_SECURITY.md), [engineering evidence](ENGINEERING_EVIDENCE.md).
