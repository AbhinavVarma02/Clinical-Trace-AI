# Agent reliability improvement

## Predeclared plan — 2026-10-17

Starting point: `70cdc31` on `feature/clinical-trace-2.0`, existing PR #1.
This work does not authorize a production deployment, Space update, merge, paid
service, or changes to private files or trained models.

The historical sample in UI_POLISH.md comprises 48 actual inference runs over
six questions: 44 LLM-composed answers, four deterministic fallbacks, and
250 drafted / 203 accepted / 47 excluded claims. The guided journey fell back
in four of eight runs; the other four had exclusions. These aggregate historical
observations are a reference, not a fresh control or independent clinical evidence.

### Acceptance criteria (recorded before implementation)

1. Guided journey: target at most one deterministic fallback in eight fresh runs
   (historical four/eight), and no malformed/truncated composer JSON. Report every
   attempt, including provider errors, refusals and quota failures.
2. Test all four UI scenarios repeatedly, plus metformin and HbA1c questions:
   eight repetitions per question if the shared free allocation permits. Target
   at most 10% excluded LLM draft claims overall (historical 18.8%) and fewer
   than six/eight warfarin runs with exclusions. Keep raw drafted, accepted and
   rejected counts, including failed LLM attempts before deterministic fallback.
3. Prefer concise answers: instruct the model to produce no more than six
   independently supported factual claims, with exact evidence identifiers.
   Do not hardcode case answers or truncate JSON into apparently valid claims.
4. Supported historical record statements, unambiguous date formats and narrowly
   equivalent unit spellings must pass explicit regressions. Adversarial variants
   with wrong dates, values, units, diagnoses, recommendations or citations must
   fail. Do not reduce the global lexical-support threshold.
5. Citation references must resolve to the correct patient and source version in
   every accepted claim. Automated citation checks do not establish semantic
   entailment; disclose that limitation and inspect representative results.
6. Safety, injection, isolation, numerical and human-review regressions must pass.
   Run the existing locked 42-case gate without tuning against held-out cases,
   Python tests/lint/scoped typing and format checks, frontend TypeScript/Vitest/
   builds, Worker checks/dry-run, and real-backend Playwright. Report exact counts
   and any unavailable checks. Target no material latency regression; report
   p50/p95 and explicitly flag a >20% increase versus a fresh comparable control.
7. Fresh before/after comparison uses the same model, questions and repetition
   count. Preflight the shared edge budget, reserve room for ordinary use, cap
   evaluation requests, retain all attempted runs and stop on quota exhaustion.
   If resources prevent eight repetitions or a fresh control, report the smaller
   sample and do not claim that the full reliability target was demonstrated.
8. Commit verified changes on the existing feature branch, push to PR #1, and
   inspect commit-specific GitHub CI. Preserve recoverable local checkpoints.

### Evaluation integrity

Development targets are the four existing UI demo questions and the historical
metformin/HbA1c questions. The held-out evaluation set remains a release gate,
not a prompt-development set. No retries are discarded, no previously generated
answer is replayed as inference, and no production abuse control is disabled.

### Recovery observations

- Actual branch and HEAD match the requested starting point; PR #1 is open.
- Baseline PR CI run `39090693843` completed successfully.
- Existing handoff files contain later dates and some stale status entries;
  their historical measurements are retained rather than silently re-dated.
- The working tree already contains a modified `.claude/launch.json`, private
  PROJECT documents and untracked UI screenshots. They are outside this change.
- The Windows virtual environment exists; initial sandboxed WSL execution failed
  before Python started. Linux Python currently lacks the project dependencies.

## Results

Baseline verification at `70cdc31`: 236 Python tests; 42 frontend Vitest tests,
TypeScript and both builds; 21 Worker tests and TypeScript; 30 real-backend
Playwright tests. The offline 42-case gate passes at 41/42, with the existing
`ho-013` lexical correctness miss retained. Python/Node dependency audits report
no known vulnerabilities; `pip check` reports no broken requirements.

The fresh edge preflight reports 2,312.8 of 8,000 neurons used. Each six-question
sample is capped at 1,000 neurons with at least 2,000 reserved globally; the
locked real-model gate has a separate bounded allowance. All calls use the
existing guarded proxy. The production edge is still `475f0b6`.

Implementation and real-model results will be recorded after verification.
