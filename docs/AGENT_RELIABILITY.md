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

### Development experiments retained

- Fresh control: the 1,000-neuron cap stopped inference after 93 physical
  requests (968.74 measured/reserved neurons). There are 47 attempted runs;
  the last run's composer was blocked by the experiment cap and the final HbA1c
  question was not attempted. Guided journey: four fallbacks/eight. Do not treat
  the cap-affected fallback as a model-quality failure or call this a complete
  48-run control. Raw artifact: `evaluation/results/reliability_before_2026-10-17.json`.
- First candidate: all 48 runs retained, 787.87 neurons, 96 requests. Guided
  journey has zero/eight fallbacks, but all eight missing-documentation answers
  fall back because the model puts factual findings in the uncertainty field.
  Counts: 154 drafted / 137 accepted / 17 excluded; one composer parse failure.
  Raw artifact: `evaluation/results/reliability_after_2026-10-17.json`.
- The next candidate explicitly treats completeness findings as citable facts,
  prioritizes journey events over lists of baseline conditions, and forbids
  unsupported laboratory interpretation. Free-form model uncertainty must pass
  verification as uncited draft statements instead of appearing unchecked in
  application caveats. All such excluded text remains visible and counted.
  The second full sample retains the same six questions, eight repetitions,
  1,000-neuron/112-request cap and 2,000-neuron ordinary-use reserve. Both
  candidate samples will be reported; this is development evaluation, not an
  independent hold-out comparison.
- The unchanged locked 42-case real-model release gate has a separate
  750-neuron/84-request limit with the same reserve. It will be run only after
  prompt/verifier changes are frozen; its cases are not used for tuning.

### Final implementation and confirmation

Implementation checkpoints: `8ca6ea2`, then final safety hardening `23ad61c`.
The composer requests concise factual claims and an empty uncertainty array;
valid generated claims are never silently sliced to improve counts. Evidence is
selected by question intent, source diversity and a whole-item character budget.
Broad journeys prioritize events/changes and limit baseline condition evidence;
explicit condition questions retain it. The planner reserves its rule-required
tools and argument scope before optional model-selected tools.

Verifier changes retain the 0.6 global lexical threshold and existing input
guards. Historical sentences require exact structured payload and event-date
support. Dates are atomic, decimal formatting is exact without rounding, and
numeric values bind to spelling-normalized units, including compound units.
Instruction-like cited content is rejected. The new encounter tool computes
intervals from actual patient encounters with versioned provenance; its checker
recomputes the ordered admission/discharge dates, day gap and window definition.
Unrecognized interval wording fails closed. Regression tests reject threshold
number borrowing, swapped date roles, invented diagnoses, unsafe advice,
fabricated citations and foreign-patient evidence.

Final confirmation was predeclared as two runs of every question plus six more
guided runs, preserving at least 2,000 shared neurons. Thus it contains **18 runs:
eight guided and two of each other question**, rather than another balanced
48-run sample. Its two source fingerprints exactly match the frozen source used
by the locked real-model gate. No prompt/verifier changes were made after that
gate. All development trials remain available; none is substituted for the final
code's smaller confirmation sample.

| Experiment | Runs | LLM composed / fallback | Drafted / accepted / excluded | Composer JSON failures |
| --- | ---: | ---: | ---: | ---: |
| Historical reference | 48 | 44 / 4 | 250 / 203 / 47 | Not separately measured |
| Fresh control, including one cap-affected run | 47 of 48 planned | 41 / 6 | 240 / 184 / 56 | 4 |
| Candidate 1, retained regression | 48 | 40 / 8 | 154 / 137 / 17 | 1 |
| Candidate 2, before final interval/evidence hardening | 48 | 48 / 0 | 231 / 225 / 6 | 0 |
| Final balanced confirmation | 12 | 12 / 0 | 58 / 51 / 7 | 0 |
| Final additional guided confirmation | 6 | 6 / 0 | 36 / 29 / 7 | 0 |
| Final combined confirmation (unequal scenario weights) | 18 | 18 / 0 | 94 / 80 / 14 | 0 |

The valid comparable control prefix is 46 complete runs, with five model-related
fallbacks; the 47th fallback is experimental budget enforcement. Candidate 2's
same first 46 positions have no fallbacks and 223/217/6 claims. This matched
development comparison **predates final hardening** and must not be reported as
the final verifier's result. Machine-readable comparisons, including provenance
and every experiment's raw output, are in
[`reliability_comparison_2026-10-17.json`](../evaluation/results/reliability_comparison_2026-10-17.json).

The matched guided scenario is more directly comparable:

| Guided journey | Frozen control (8) | Final code (8) |
| --- | ---: | ---: |
| Deterministic fallbacks | 4 | 0 |
| Composer JSON failures | 4 | 0 |
| LLM drafted / accepted / excluded | 42 / 14 / 28 | 48 / 38 / 10 |
| Latency p50 / p95 | 5.408 / 7.474 s | 3.402 / 4.721 s |
| Citation patient/version resolution errors | 0 | 0 |

**Acceptance assessment:** the guided fallback/JSON target is met in this finite
sample. The final exclusion target is **not met**: 14/94 (14.9%) overall and
10/48 (20.8%) in the guided case; every final guided answer remains partially
verified. Warfarin improved in Candidate 2's eight runs but still loses one claim
in each of the two final runs, so a stable eight-run improvement is not established
for final code. The model also sometimes exceeds the requested six-claim limit
(eight well-cited missing-documentation findings); all entries remain counted.

Remaining exclusions include narrow interval grammar, supported but lexically
different words such as "referred"/"identified", and genuinely unsupported INR
reference-range assertions. No threshold was lowered to accept them. Manual
inspection also finds incomplete flagship coverage: only two of eight final
accepted answers explicitly mention readmission, and none mentions metformin.
These string-presence observations are not semantic correctness scores; they
show that reliable JSON alone does not guarantee the ideal reviewer narrative.
There is no claim of complete success or uniform answer-quality improvement.

### Release gates and verification

| Check | Actual result |
| --- | --- |
| Full Python suite | **390 passed**, 44.79 s |
| Frontend | **42 Vitest tests**, TypeScript and both builds passed |
| Worker | **25 tests**, TypeScript, deploy dry-run and dependency audit passed |
| Final real-backend Playwright | **30 passed**, 58.0 s; desktop/tablet/mobile, review, isolation, streaming and recovery |
| Ruff / format / typing | Repository lint; scoped format; five contract/persistence/telemetry/evaluator modules passed mypy |
| Secret / dependency checks | No secret-pattern findings; pip check clean; Python and Node audits found no known vulnerabilities |
| Protected files | All **26** pre-recorded hashes unchanged; original unrelated worktree changes preserved |
| Locked offline 42-case gate | **PASS, 41/42**; existing `ho-013` lexical correctness miss retained; critical safety 1.0 |
| Locked real-model 42-case gate | **PASS, 37/42**, correctness 0.7826; 94 drafted, 78 retained, 16 excluded; 2 fallbacks; critical safety 1.0 |
| Real-model safety/isolation | Zero unsafe accepted outputs, isolation violations or authorization violations; schema/citation coverage 1.0 |
| GitHub CI | Inspected after the final push; commit-specific outcome is recorded in the completion report and PR #1 |

The real-model gate's five quality misses are `dev-001`, `dev-004`, `dev-005`,
`ho-010`, `ho-013`. Its 37/42 and correctness 0.7826 are below the historical
39/42 and 0.870 reference; that historical run is not a fresh paired control.
Its excluded-draft rate is 16/94 (17.0%). The thresholds/scoring were unchanged;
these failures were recorded without tuning against held-out cases. Raw reports:
[`offline`](../evaluation/results/reliability_gate_offline_2026-10-17.json),
[`real model`](../evaluation/results/reliability_gate_llm_2026-10-17.json).

All accepted citations in the final scenario confirmations resolve to same-run,
same-patient evidence and the actual fixture source version. Derived findings
are recomputed for lineage checks. This is reference accuracy, **not proof of
clinical entailment**; general entity/value/date relationships remain heuristic.
Latency includes local tools, network inference and read-only quota accounting;
the unequal final scenario weights preclude a fair overall-latency comparison.

All six retained live experiments together used **3,431.97 reported/estimated or
conservatively reserved neurons**, leaving approximately **2,255 of 8,000** for
ordinary use. No paid service or production abuse control was changed. Live
experiments stop here to preserve that reserve. Public edge remains `475f0b6`;
the Space, main branch, private configuration, models and processed data were not
changed by this continuation.

### Reproduction and next work

Use the existing configured runtime and a token supplied through an environment
variable; never put its value in a command, report or commit:

```bash
python -m evaluation.reliability --provider cloudflare-workers-ai \
  --edge-origin https://clinical-trace-ai.room-for-both.workers.dev \
  --model @cf/qwen/qwen3-30b-a3b-fp8 \
  --api-key-env CLINICAL_TRACE_LLM_API_KEY \
  --repetitions 8 --max-neurons 1000 --max-calls 112 --reserve-neurons 2000 \
  --source-revision <verified-commit> --label <new-label> --output <new-report.json>
```

The default workload is all six questions in repetition-major UI order; use
`--scenario readmission-journey` for a declared guided-only experiment. Existing
reports cannot be overwritten. Omit provider/origin to run the same workload
offline without inference charges. Source hashes identify dirty source exactly;
reports also preserve each raw provider attempt, parse failure, final result,
rejected claim and usage snapshot. The public proxy still reports synthetic
`finish_reason=stop`; local Worker code now preserves reported metadata or marks
it unknown, but that Worker change has **not been deployed**.

Next work should evaluate typed selection of computed statements, improve bounded
interval paraphrases with independent counterexamples, and measure flagship
fact coverage/duplicate claims on a larger independently authored workload. A new
balanced repeat sample needs available free quota. Human annotation/agreement
and clinical validation remain unmeasured; no new model, paid activation, merge
or deployment is authorized by this report.


## Focused guided completeness continuation

The final focused change at `e412405` fixes timeline-first evidence metadata and
prioritizes documented events in broad summaries. All seven completed LLM
answers cover both readmission and the medication stop; 42/42 generated claims
verify. The eighth composer was blocked by the predeclared cap and fell back:
**1/8 fallbacks**, so the complete-eight-run/zero-fallback targets remain unmet.
409 local Python, 42 frontend, 25 Worker and 30 browser tests pass; fresh offline
gate 41/42. The previous real-model 37/42 gate is historical; no fresh full model
gate fits the preserved reserve. Complete comparison, independent fixtures and
limitations: [GUIDED_COMPLETENESS.md](GUIDED_COMPLETENESS.md). Unreleased.
