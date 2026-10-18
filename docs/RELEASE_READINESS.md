# Final release readiness

Continuation date: 2026-10-17 (America/New_York). Starting branch
`feature/clinical-trace-2.0`, HEAD `ff0a614bd7cbfff8863690ed3646486d1b1ed4d4`.
PR #1 is open/unmerged; starting CI run `39094562508` completed successfully.
Deployment, merging, paid services and production credential changes remain
**unauthorized**. Earlier deployment dates are preserved historical records.

## Recoverable phase status

- A — COMPLETE locally: scoped daily allowance/status, reset and temporary-limit
  UX; 50 frontend tests, 30 Worker tests, 36 real-backend browser tests pass.
- B — **PENDING — QUOTA BLOCKED**. Read-only production preflight at
  `2026-10-18T03:54:14.867Z`: 5,909.2 / 8,000 neurons used; 2,090.8 left,
  only 90.8 above the protected 2,000 reserve. The existing full-gate harness
  declares a 750-neuron / 84-call maximum; this allocation cannot fund it.
  No model calls were made. Do not retry until a new allocation is checked.
- C — IN PROGRESS: coordinated release checklist and read-only public smoke.
  Production status: edge `475f0b6`, backend ready, real model configured.
- D — IN PROGRESS: fresh 409 Python tests, repository lint, seven-module mypy,
  frontend TypeScript/both builds, Worker typecheck/deploy dry-run pass.
  Fresh locked offline gate PASS, 41/42 (known `ho-013` scoring miss); safety
  1.0, citation coverage/precision 1.0, no isolation/authorization/unsafe output
  violations, fallback rate 0, p50/p95 21.73/37.16 ms. Raw new report:
  `evaluation/results/release_readiness_gate_offline_2026-10-17.json`.
  Dependency/format checks and head-specific CI remain pending.

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

Production read-only baseline: **7/7 passed** at `2026-10-18T03:58:13.569Z`;
Space metadata pins running `9afa0f8dfe602f42a8a7de3b7ac328da08966de9`,
hardware `cpu-basic`, and v1 `/_stcore/health` returned `ok`.

## Next action

Commit/push the verified Phase A milestone. Finish rollback orchestration tests,
stage/verify the coordinated package locally, complete security/format checks,
then confirm all five CI jobs on the final pushed head. Live evaluation and a
fresh public full LLM smoke remain quota-blocked. No deployment or merge is
authorized. Keep all failed attempts and existing evaluations.
