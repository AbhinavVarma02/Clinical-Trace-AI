# Reviewer-ready deployment and validation handoff

The continuation from f4f785c preserves v1 and reaches a tested local core milestone. This is a handoff for a **separate deployment/validation phase**, not a declaration that the full public-release definition of done is met.

## Source and evidence

Use branch `feature/clinical-trace-2.0`; inspect `git status` and pin `git rev-parse HEAD` before validation. Do not reset private/uncommitted files. Core source 6541d9b passed all four jobs in Actions 39077304115. Its actual local 7B evaluation passed 35/42 with negative results preserved. Subsequent calibration-evidence and human-review protocol follow-ups have local tests; inspect the latest branch CI for exact-commit portability evidence.

Current local evidence: 216 Python tests; 36 frontend tests plus TypeScript/two builds; 24 real-backend desktop/tablet/mobile browser cases; lint, scoped format/type gates; 144 audited runtime packages with no known findings; unchanged 41/42 deterministic gate; versioned readmission/drift and performance artifacts. See PROJECT_STATUS.md, ENGINEERING_EVIDENCE.md and the evaluation result JSONs. Tool/provider mocks in unit tests are separate from the real local Ollama run and real browser/backend tests.

## Required release validation

1. Reproduce from a fresh clone with pinned roots/constraints and development requirements; build both frontends, run Python/frontend/browser/evaluation/security/type/format checks and inspect CI for the pinned commit.
2. Build/run the container with one worker per SQLite file; verify non-root startup, readiness, persistence across restart, no secrets in responses/logs, actual agent execution, citations, reviews, unavailable/available risk paths and prior v1 APIs. Measure true image size/cold start/native memory on available infrastructure; current in-process timings do not substitute.
3. Select a genuinely free independent 2.0 public target and inference option with explicit owner authorization/credentials. **Keep the existing public Hugging Face v1 demo unchanged.** Do not merge main, rewrite history, activate paid services or overwrite a deployment.
4. Benchmark actual selected public inference: fresh questions, grounded tools, unsupported evidence, provider failures, quotas, CPU/memory and latency. The local 7B gate is not a public-CPU responsiveness claim; the historical 1.5B quality failure remains a failure.
5. From a new unauthenticated browser, verify load, patient selection/resources, fresh questions and real tool execution, every citation's exact version, patient/session isolation, all reviewer actions/version invalidation, actual risk/SHAP path, errors, keyboard focus, mobile/tablet, themes and safe log content. Inspect representative screenshots and long/loading/empty/error states.
6. If human evaluation is performed, collect independent complete paired annotations using REVIEW_VALIDATION_PROTOCOL.md. No clinical validation or human agreement exists yet. Record real outcomes, including failures, and make a reviewed release decision against predeclared gates.

## Remaining conditional extensions

The roadmap preserves sequence-model research (encounter order is only a proxy), neural biomedical/general embedding comparison, optional reranking, optional PostgreSQL/pgvector, simulated SMART launch, optional kind manifests, deeper memory/stress/scaling experiments and structured synthetic referral/discharge extraction with uncertainty/provenance. These are not falsely completed by the local core milestone and should be scheduled according to dependencies and measured value.

## Recovery and operational limits

Use one application worker per SQLite DB. Interrupted runs are never auto-replayed: owner-only resume checks source/dependency/model/provider/data compatibility and permits at most three attempts. Cancellation is cooperative. Review-bound runs are held during explicit retention pruning. External OpenTelemetry trace files require separate rotation. Unknown hosted costs remain unknown; no paid integrations were activated.

Private workspace continuity lives in ignored `.project-state/`; a fresh clone can reconstruct engineering status from this document, PROJECT_STATUS.md, IMPLEMENTATION_ROADMAP.md, tests and artifacts. Public v1 and existing trained artifacts were preserved throughout this continuation.
