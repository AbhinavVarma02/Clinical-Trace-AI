# Project Status

**Updated:** 2026-11-07 · **Stable:** `main` (2.0 public demo; last recorded release 2026-10-18) · **Beta development:** branch `feature/beta` (local and test-only; not deployed) · **Epic M1:** Draft PR #2, unmerged.

This page is the portable, version-controlled handoff. It contains no credentials, private Epic snapshot content or machine-specific evidence. Detailed private checkpoints stay in the git-ignored `.project-state/` folder of the working copy that produced them; a fresh clone does not have them. The Stable section further down is kept unchanged as the record of 2026-10-18.

## Current status (Beta channel, as of 2026-11-07)

| Stream | State |
|---|---|
| Stable 2.0 public demo | Merged to `main` (PR #1). Public edge https://clinical-trace-ai.vathadiabhinav.workers.dev with the HF Space co-host; the v1 dashboard is unchanged. Last recorded release 2026-10-18; not re-verified for this update. None of the Beta changes below are deployed. |
| Epic M1 sandbox adapter | Complete (all nine gates). Command-line only, read-only, Epic test patients only. Draft PR #2 stays unmerged ([EPIC_SANDBOX.md](EPIC_SANDBOX.md), [ADR 0008](ADR/0008-epic-sandbox-source-adapter.md)). |
| M2-S1 data-source registry | Committed and pushed on `feature/beta`; hosted CI green. Off by default; an Epic snapshot can be served locally as an isolated, deterministic-only extra source ([ADR 0009](ADR/0009-beta-channel-and-data-sources.md)). |
| M2.2-a discharge-summary match (P2) | Committed and pushed; hosted CI green. A note counts as a discharge summary by LOINC 18842-5 or its label in any letter case. |
| M2.2-b medication coverage provenance | Committed on `feature/beta`. When this page was written it had not been pushed and had no hosted CI run; pushing the commit that adds this page publishes M2.2-b with it. |
| ISSUE-7 | **Open, unresolved.** Records that declare no medication import coverage (the synthetic fixture) still cite the Patient for "no medication orders"; see [LIMITATIONS.md](LIMITATIONS.md). Changing it would alter approved synthetic answers and needs an explicit decision. |

### Git state when this page was written

| Ref | Commit |
|---|---|
| `main` (remote) | `72ae8c8`: Stable 2.0 (PR #1, merged) and dependency security fixes (PR #3, merged) |
| `feature/epic-sandbox-readonly` | `028e609`: Epic M1, Draft PR #2 into `main`, unmerged |
| `feature/beta` | `83abb78` M2-S1 · `0fd3c75` CI lists · `183dad1` M2.2-a (last pushed commit) · `ee76a58` M2.2-b (local) · then the commit that adds this page |

There is no pull request from `feature/beta`.

### M2.2-b behaviour (records with no medication orders)

A data source's declared medication import coverage is query/import provenance, never clinical evidence. When coverage is declared, the Patient resource is never cited as proof that orders do not exist.

| Declared coverage | Completeness finding (record view) | Answers (summary, missing documentation, review) |
|---|---|---|
| none declared (synthetic fixture) | kept | unchanged: Patient-cited gap claim (ISSUE-7, unresolved) |
| `not_imported` | not made | uncited "not imported" note; the readmission note says the orders were not imported for this record |
| `returned_none` | kept, labelled with its coverage | uncited "the data source returned no medication orders" note instead of a gap claim |
| unrecognised value | kept, labelled with its coverage | uncited "no medication orders are recorded" note plus the "absence does not show the patient takes no medication" caveat |

Other completeness findings are unchanged. Changed files: `src/clinical/records.py`, `src/clinical/risk_bridge.py`, `src/agent/tools.py`, `src/agent/composer.py`, four test files (one new) and two docs.

### Test evidence

Local runs are offline and deterministic (no network, no language model, tracing off).

| Check | Result |
|---|---|
| Python suite at M2.2-b (local) | 770 passed |
| Evaluation gate at M2.2-b (local) | Pass, 41/42 (the known `ho-013` miss); metrics identical to M2.2-a |
| Synthetic demo answers | Byte-identical to M2.2-a (4 patients × 6 questions, plus medication questions) |
| Default-mode API | 0 differences across 97 responses against the Epic M1 baseline (`028e609`) |
| Lint, types, formatting, secret scan (local) | Clean |
| Hosted CI on `feature/beta` | All five jobs green for M2-S1 ([42320483550](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42320483550), [42321659856](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42321659856)) and M2.2-a ([42326596478](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42326596478)); M2.2-b had not run when this page was written |

### Remaining Beta milestones

Each step needs its own approval and lands as its own commit with its own CI.

1. **M2.2-c (next):** disclose DiagnosticReport result references that do not resolve in the record, cited to the report itself; synthetic answers unchanged.
2. **M2.2-d:** medication vocabulary (L4), including an owner-approved anticoagulant class list, without changing retrieval.
3. **M2.2-e:** show the note-versus-encounter admission-date conflict (ISSUE-5) in encounter and summary answers.
4. **M2.2-f:** answer relevance (ISSUE-6), measured against the evaluation and synthetic goldens before any change.
5. **ISSUE-7:** owner decision on the synthetic Patient-cited gap.
6. **Before a Beta deployment (provisional roadmap):** medication-date handling and medication import with a live test gate (A1); Epic encounter-class mapping (only with documentation); more than one Epic test patient; coverage shown in answers and the UI; then the deferred hosting, account, public-content and UI decisions. Public exposure, if ever, would ship only a static pre-imported snapshot.

### Approval boundaries

- Approved: Beta work on `feature/beta` (task branches allowed); Beta and Epic sources deterministic-only; data sources tested locally with synthetic and MOCK Epic fixtures only; ADR 0009. The phased roadmap is provisional and is not deployment authority.
- Deferred, not authorised: Beta backend host, Cloudflare account placement, public Epic content, Beta UI.
- Every commit, push, pull request, merge and deployment needs separate owner approval. So do live Epic or language-model calls, cloud resources, any UI change, replaying a private snapshot, and starting the next milestone.
- `main`, the Stable demo, production and Draft PR #2 stay unchanged.

### Development workflow

1. Inspect Git state and this page, then the private checkpoint if available.
2. Agree the exact scope and stop at every approval gate.
3. Write tests first for any clinical change and show that they fail on the unchanged code.
4. Implement narrowly; no UI, deployment, retrieval or evaluation changes unless approved.
5. Run the full offline regression: Python suite, evaluation gate, synthetic goldens, default-mode API comparison, lint, types, formatting, secret scan. Tracing and language-model providers stay off; Epic data stays MOCK unless approved.
6. Review the exact diff; commit only the reviewed paths, without attribution trailers.
7. After an approved push, verify every hosted CI job and step.
8. Update this page and the private handoff after every milestone.

### Continuing in another tool or session (Codex, a new chat, another account)

1. Open the working copy on `feature/beta`. Run `git status -sb`, `git log --oneline -6` and `git ls-remote origin refs/heads/feature/beta`, and compare with the Git state above.
2. If the working copy has `.project-state/HANDOFF.md`, read its top "CANONICAL RESUME POINT" section; it holds the exact commands, evidence paths and a ready-to-paste prompt. Otherwise use this page, [EPIC_SANDBOX.md](EPIC_SANDBOX.md), [LIMITATIONS.md](LIMITATIONS.md) and [ADR 0008](ADR/0008-epic-sandbox-source-adapter.md) / [ADR 0009](ADR/0009-beta-channel-and-data-sources.md).
3. Before running anything in Python, set `CLINICAL_TRACE_SKIP_DOTENV=1` and `LANGCHAIN_TRACING_V2=false`: a local `.env` can enable trace upload. Keep the language-model provider unset.
4. Present a short plan for the next approved step and wait for approval before changing files.

## Stable 2.0 status (as of 2026-10-18)

Historical record, kept unchanged. Since then PR #1 has been merged into `main`; the branch and "No merge to `main`" in the next line describe 2026-10-18.

**Updated:** 2026-10-18 · **Version:** 2.0.0-dev · **Branch:** `feature/clinical-trace-2.0` · **Public:** https://clinical-trace-ai.vathadiabhinav.workers.dev (dedicated Cloudflare Free account, full workflow, live LLM; see [CLOUDFLARE_ACCOUNT_MIGRATION.md](CLOUDFLARE_ACCOUNT_MIGRATION.md)) · v1 dashboard unchanged at the same Space URL. No merge to `main`.

The reviewer-ready deployment phase is complete: the final release gate (complete primary agent workflow through the unauthenticated public URL) passed on desktop and mobile, including a backend cold start. See [PUBLIC_DEPLOYMENT.md](PUBLIC_DEPLOYMENT.md) and [ADR 0007](ADR/0007-public-edge-and-inference.md). The full master plan is not complete: human-review agreement, clinical validation and conditional advanced extensions remain.

The unreleased guided-completeness follow-up at `e412405` preserves canonical
structured evidence and prioritizes important documented events. All seven
completed LLM answers mention and verify both events (42/42 claims); the eighth
composer was blocked by the declared budget and fell back, so the zero-fallback
and complete-eight-run confirmation targets remain unmet. Local gates: 409
Python, 42 frontend, 25 Worker and 30 browser tests; fresh offline gate 41/42.
The previous real-model 37/42 gate remains historical; a fresh full model gate
was not funded within the reserve. See [GUIDED_COMPLETENESS.md](GUIDED_COMPLETENESS.md)
and the retained earlier [AGENT_RELIABILITY.md](AGENT_RELIABILITY.md) experiments.
The public edge and Space were not updated during either continuation.

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
| Free hosted inference | Workers AI Qwen3-30B-A3B relayed by the edge; recorded 42-case gate pass 39/42 (correctness 0.870) at `d00dbc7`; 8,000-neuron daily cap; quota exhaustion degrades to labelled deterministic answers |
| Public release gate | 14/14 through the public URL; desktop 10.9 s and mobile 7.5 s to a verified live-LLM answer; review approved; cold start recovered; 0 availability errors in `/edge/metrics` |
| Public-mode isolation | Random per-browser workspaces scope runs, sessions, reviews, feedback and audit; backend accepts only edge traffic |
| Reviewer experience | Guided four-step case, availability indicator, queued questions during cold starts, automatic recovery, static evidence snapshot with source hashes |
| Containers | 2.01 GB image; ready 16.6 s (2 vCPU) / 248.8 s (0.1 vCPU, 512 MB); peak about 296 MiB; co-hosted v1+2.0 image verified 9/9 |
| Local performance | 40 requests each at concurrency 1/4/8; at four: p95 248.9 ms, 40/40 answered; at eight: 28/40 capacity rejections, 12 answered; zero observed isolation violations |

## Reproducible checks

| Check | Result / scope |
| --- | --- |
| Python suite | **409 passed**, including composer/verification adversarial regressions, public-mode isolation, quota degradation, inference relay and evidence-snapshot provenance |
| Frontend `npm run check` | **42 tests**, TypeScript and both builds passed |
| Edge Worker `npm run check` | **25 tests** and TypeScript; local deploy dry-run and dependency audit passed |
| Playwright | **30 real-backend Chromium cases**, desktop/tablet/mobile; adds the guided case and a simulated cold-start recovery |
| Ruff / format / mypy | Lint passed; formatting scoped to new modules; mypy explicitly covers four new contract/persistence/telemetry modules |
| Deterministic evaluation | Gate PASS; 41/42 cases; `ho-013` lexical miss retained; no known test isolation/authorization violations |
| Actual local 7B inference | At `6541d9b`: gate PASS, 35/42, correctness 0.7391, rejected draft-claim rate 0.2308, p50 2.673 s/p95 8.018 s; historical reports retained |
| Linux CI/container | [Actions 39077304115](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/39077304115) at `6541d9b`: all four jobs passed, including real browsers, mandatory security audit and actual container build/smoke |

The latest branch run is available in [GitHub Actions](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions?query=branch%3Afeature%2Fclinical-trace-2.0). Keep commit-specific evidence separate from subsequent changes. The earlier run 39074327485 also passed, but its advisory security step contained findings; those were inspected and fixed, not treated as a clean audit.

## Remaining work and constraints

- Hosted inference quality varies run to run (36-39/42 across code versions and runs); the recorded run is the reference. Live inference needs streamed requests.
- Free hosting: the Space sleeps after 48 idle hours (wake time unmeasured); SQLite on the Space is ephemeral, so public runs and reviews disappear on restart.
- Readmission scores remain poorly calibrated (Brier 0.134, ECE 0.190); disclosed in the UI and docs.
- Human-review agreement is unmeasured; no clinical validation exists.
- Conditional advanced work remains: sequence models, biomedical embeddings, reranking/PostgreSQL, simulated SMART launch, kind/Kubernetes, deeper stress tests and structured referral/discharge extraction.

Reproduction and evidence: [public deployment](PUBLIC_DEPLOYMENT.md), [requirements reconciliation](REQUIREMENTS_TRACEABILITY.md), [reliability](RELIABILITY.md), [readmission audit](READMISSION_ANALYSIS.md), [dependency security](DEPENDENCY_SECURITY.md), [engineering evidence](ENGINEERING_EVIDENCE.md).
