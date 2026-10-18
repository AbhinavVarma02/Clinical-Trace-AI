# Final release readiness

Continuation began 2026-10-17; updated 2026-10-18 (America/New_York). Starting branch
`feature/clinical-trace-2.0`, HEAD `ff0a614bd7cbfff8863690ed3646486d1b1ed4d4`.
PR #1 is open/unmerged; starting CI run `39094562508` completed successfully.
Deployment, merging, paid services and production credential changes remain
**unauthorized**. Earlier deployment dates are preserved historical records.

## Recoverable phase status

- A — COMPLETE locally: scoped daily allowance/status, reset and temporary-limit
  UX; 52 frontend tests, 30 Worker tests, 36 real-backend browser tests pass.
- B — **PENDING — QUOTA BLOCKED**. Read-only production preflight at
  `2026-10-18T03:54:14.867Z`: 5,909.2 / 8,000 neurons used; 2,090.8 left,
  only 90.8 above the protected 2,000 reserve. The existing full-gate harness
  declares a 750-neuron / 84-call maximum; this allocation cannot fund it.
  No model calls were made. Do not retry until a new allocation is checked.
- C — PREPARED/LOCALLY VERIFIED: [coordinated release checklist](RELEASE_CHECKLIST.md),
  read-only public smoke 7/7, staged co-host Docker verification 9/9, v1 health
  and exact Worker/Space rollback pins. Fresh public full inference smoke remains
  quota-blocked; production is unchanged. Worker rollback is automatic; Space/D1
  recovery has separate explicit commands and limitations.
- D — LOCAL CHECKS COMPLETE: fresh **415 Python tests** (44.51 s), repository
  lint, seven-module mypy,
  frontend TypeScript/both builds, Worker typecheck/deploy dry-run pass.
  Fresh locked offline gate PASS, 41/42 (known `ho-013` scoring miss); safety
  1.0, citation coverage/precision 1.0, no isolation/authorization/unsafe output
  violations, fallback rate 0, p50/p95 21.73/37.16 ms. Raw new report:
  `evaluation/results/release_readiness_gate_offline_2026-10-17.json`.
  Both Node dependency audits report 0 vulnerabilities; pip-audit reports no
  known vulnerabilities; pip check clean. Both CI format scopes (35 modules)
  pass; final tracked/candidate secret scan **297 files, 0 findings**. The six new
  host-restoration cases pass; Worker suite includes 30 Vitest + 6 isolated Node
  rollback tests. Release-tool CI `39096141734` passed all five jobs, including
  the actual container build/smoke. Linux Python: 412 passed / 3 existing skips
  (unbuilt v1 component asset and two absent-model cases); local: 415 passed.
  Final UI/documentation head confirmation is recorded in the private handoff.

## Preserved evidence and files

Previous live guided trial: seven LLM answers, 42/42 verified claims; eighth
composer cap-blocked and fell back. No fresh full real-model evaluation exists
for `e412405`; prior 37/42 is historical. Locked cases/thresholds stay unchanged.
See [GUIDED_COMPLETENESS.md](GUIDED_COMPLETENESS.md).

All 26 recorded protected hashes match at recovery. Original unrelated changes:
`.claude/launch.json`; untracked `PROJECT_DOCUMENTATION.md`,
`PROJECT_DOCUMENTATION.pdf`, `PROJECT_MASTER_GUIDE.md`,
`PROJECT_MASTER_GUIDE.backup-2026-10-08.md`, and three badge screenshots under
`docs/images/ui-polish/`. Do not stage or overwrite these. Private runtime and
recovery details live in the existing ignored `.project-state/` directory.

## Phase A implementation

Status reads the gateway's daily salted connection identity, reports visitor and
deployment counters separately, and returns null when visitor identity/accounting
is unavailable. Old-Worker responses remain compatible. No quota or enforcement
rule changed. Existing accounting counts gateway attempts, including rejected
requests; this is disclosed. Shared connections share the visitor allowance.

Daily 429 errors include scope, reset time and Retry-After; temporary limits
identify agent/API scope. The client retains those errors independently of stale
status, disables run controls, and recovers after expiry/status refresh without
automatically replaying rejected execution. Daily run limits take priority over
inference fallback messaging. Browsing/review controls remain usable. Known-edge
HTTP/network failures cannot silently switch to direct mode. Tests cover daily
exhaustion, UTC rollover, visitor identity, unknown accounting, temporary limits,
stale status, recovery and desktop/tablet/mobile browsing and execution.
Final review also corrected section-level retry recovery: evidence/review errors
hide manual retries only until their own Retry-After/reset expires. This is
separate from agent allowance blocking; two added timer tests cover both cases.

Production read-only baseline: **7/7 passed** at `2026-10-18T03:58:13.569Z`;
Space metadata pins running `9afa0f8dfe602f42a8a7de3b7ac328da08966de9`,
hardware `cpu-basic`, and v1 `/_stcore/health` returned `ok`.
Final read-only status at `2026-10-18T04:14:16.163Z` confirms the same edge,
ready backend, and unchanged 5,909.2-neuron usage. This continuation made **zero
inference calls**. All 26 protected-file hashes still match; only the original
unrelated working-tree files remain.

## Release recovery improvements

The release runner now refuses to publish without an exact previous Worker
version. It captures its receipt before mutation, stops on migration failure,
rolls back on deploy or smoke error, and reports rollback failure explicitly.
Six isolated Node tests pass. Full smoke requires genuine LLM planner/composer
and rejects deterministic fallback. No live release/rollback drill was run.

`restore-cohost` restores an exact prior HF tree in one parent-pinned commit
without removing relay settings/secrets. It requires owner approval and the
failed publication's exact SHA; races/v1-only targets fail closed. The old
v1-only rollback remains available. New mocked tests verify this without any
Space mutation. The staged backend/frontend at `be52df7` passed 9/9 checks;
the later restoration-tool change is host administration only.

## Commits and CI

`be52df7` (Phase A), `3c61bc2` (release tools/reports) and `b1ce0b9`
(section-retry recovery) are pushed to the existing feature branch and PR #1.
[Phase A CI](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/39095723473)
and [release-tool CI](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/39096141734)
completed SUCCESS. The UI recovery run is
[39096349237](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/39096349237).
The final documentation commit's exact HEAD/run/completion and PR-description
result are saved in `.project-state/HANDOFF.md` after remote confirmation;
inspect [current branch CI](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions?query=branch%3Afeature%2Fclinical-trace-2.0)
for newer heads. Starting `ff0a614` CI is historical evidence only.

## Next action

**Release readiness: HOLD** for the fresh locked live gate and candidate public
full LLM smoke; neither can be certified by offline tests or historical results.
No deployment or merge is authorized. Resume by reading this report,
`.project-state/HANDOFF.md` and the checklist, then inspect branch/HEAD/status,
PR #1 and its head-specific CI. Check a new free allocation once before any
future inference. Keep existing attempts; do not repeat completed evaluations.
The private `.project-state/run_release_live_gate.py --output <new-report.json>`
runner checks a full 750-neuron allocation above reserve before calling the
unchanged locked evaluator, caps requests at 84, and retains failed attempts.
Do not use old runners that point at existing report paths. PR description edits
may still be blocked by the integration's previously observed 403; save prepared
text in `.project-state/PR_RELEASE_READINESS_BODY.md` if that recurs.
