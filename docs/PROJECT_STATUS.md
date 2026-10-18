# Project Status

**Updated:** 2026-10-18 · **Version:** 2.0.0-dev · **Branch:** `feature/clinical-trace-2.0` · **Public:** 2.0 edge live at https://clinical-trace-ai.room-for-both.workers.dev (workspace, measured evidence, Workers AI inference); **public backend pending owner approval**; v1 Space unchanged. No merge to `main`.

The local core is implemented and verified, and the reviewer-ready deployment phase is in progress. The public demo is **not ready**: the release gate requires the complete agent workflow through the public URL, which needs a public backend host. Hugging Face no longer lets free accounts create Docker Spaces, so the options (recommended: reversible co-hosting in the existing Space) await the owner. See [PUBLIC_DEPLOYMENT.md](PUBLIC_DEPLOYMENT.md) and [ADR 0007](ADR/0007-public-edge-and-inference.md).

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
| Public edge (Cloudflare Workers Free) | Static workspace, allow-listed gateway, per-visitor rate limits and daily caps, D1 accounting, `/edge/status` and `/edge/metrics`; release script with smoke-gated auto-rollback; live rollback drill 5 s / 7 s |
| Free hosted inference | Workers AI Qwen3-30B-A3B via authenticated proxy; recorded 42-case gate pass 36/42 (correctness 0.826); deployed-proxy run 37/42; 8,000-neuron daily cap; quota exhaustion degrades to labelled deterministic answers |
| Public-mode isolation | Random per-browser workspaces scope runs, sessions, reviews, feedback and audit; backend accepts only edge traffic |
| Reviewer experience | Guided four-step case, availability indicator, queued questions during cold starts, automatic recovery, static evidence snapshot with source hashes |
| Containers | 2.01 GB image; ready 16.6 s (2 vCPU) / 248.8 s (0.1 vCPU, 512 MB); peak about 296 MiB; co-hosted v1+2.0 image verified 9/9 |
| Local performance | 40 requests each at concurrency 1/4/8; at four: p95 248.9 ms, 40/40 answered; at eight: 28/40 capacity rejections, 12 answered; zero observed isolation violations |

## Reproducible checks

| Check | Result / scope |
| --- | --- |
| Python suite | **228 passed**, including public-mode isolation, edge secret, quota degradation and evidence-snapshot provenance |
| Frontend `npm run check` | **40 tests**, TypeScript and both builds passed |
| Edge Worker `npm run check` | **19 tests** and TypeScript; deploy dry-run and dependency audit in CI |
| Playwright | **30 real-backend Chromium cases**, desktop/tablet/mobile; adds the guided case and a simulated cold-start recovery |
| Ruff / format / mypy | Lint passed; formatting scoped to new modules; mypy explicitly covers four new contract/persistence/telemetry modules |
| Deterministic evaluation | Gate PASS; 41/42 cases; `ho-013` lexical miss retained; no known test isolation/authorization violations |
| Actual local 7B inference | At `6541d9b`: gate PASS, 35/42, correctness 0.7391, rejected draft-claim rate 0.2308, p50 2.673 s/p95 8.018 s; historical reports retained |
| Linux CI/container | [Actions 39077304115](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/39077304115) at `6541d9b`: all four jobs passed, including real browsers, mandatory security audit and actual container build/smoke |

The latest branch run is available in [GitHub Actions](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions?query=branch%3Afeature%2Fclinical-trace-2.0). Keep commit-specific evidence separate from subsequent changes. The earlier run 39074327485 also passed, but its advisory security step contained findings; those were inspected and fixed, not treated as a clean audit.

## Remaining work and constraints

- **Release blocker:** public backend host (owner decision). After approval: publish, configure `BACKEND_ORIGIN`, run `npm run release -- --full`, validate desktop/mobile as an unauthenticated visitor and record public agent completion, error rate and cold-start recovery from `/edge/metrics`.
- Hosted inference quality varies run to run (36-37/42); the recorded run is the reference.
- Readmission scores remain poorly calibrated (Brier 0.134, ECE 0.190); disclosed in the UI and docs.
- Human-review agreement is unmeasured; no clinical validation exists.
- Conditional advanced work remains: sequence models, biomedical embeddings, reranking/PostgreSQL, simulated SMART launch, kind/Kubernetes, deeper stress tests and structured referral/discharge extraction.

Reproduction and evidence: [public deployment](PUBLIC_DEPLOYMENT.md), [requirements reconciliation](REQUIREMENTS_TRACEABILITY.md), [reliability](RELIABILITY.md), [readmission audit](READMISSION_ANALYSIS.md), [dependency security](DEPENDENCY_SECURITY.md), [engineering evidence](ENGINEERING_EVIDENCE.md).
