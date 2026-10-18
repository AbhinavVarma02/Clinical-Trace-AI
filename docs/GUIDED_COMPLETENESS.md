# Guided-case completeness — focused continuation

## Acceptance criteria recorded before implementation

Starting source: `90e9ac6`, existing `feature/clinical-trace-2.0`, PR #1.
The preceding final guided sample had zero/eight fallbacks, two/eight accepted
answers mentioning readmission, none mentioning metformin, 48/38/10 LLM claims
drafted/accepted/excluded, and median latency 3.402 seconds.

This is one focused implementation and one predeclared live confirmation, not
a search over model responses. Preserve every attempt and do not tune against
the locked held-out cases. Author separate synthetic completeness/adversarial
fixtures with different patients, medications, dates and admission gaps before
implementation; mocks exercise contracts, not real model quality.

Targets for eight new guided runs:

- At least seven/eight accepted answers cover the documented return admission,
  and at least seven/eight cover the documented medication stop. Report both
  mentions and citation-grounded coverage; identifiers alone do not prove a fact.
- Zero deterministic fallbacks and zero composer JSON failures. Target no more
  than six drafted claims per answer, without silently trimming overproduction.
- Every accepted citation resolves to same-run, same-patient, exact-version
  evidence. Wrong gaps/date roles, fabricated events, stale medication status,
  treatment advice, injection and foreign citations must remain rejected.
- Keep the verifier and safety thresholds unchanged. Prefer correcting evidence
  identity/ranking and composition over expanding verification exceptions.
- Flag a guided median latency increase above 20% relative to the previous
  3.402-second sample. This is a historical development comparison, not a fresh
  paired control or clinical validation.
- Run the full Python/offline locked gate, frontend, Worker, browser and CI gates.
  Record actual failures and artifact-dependent skips. Never substitute a prior
  real-model gate for a new one without clearly labeling it historical.

Free-budget preflight: 5,745 of 8,000 neurons used, leaving about 255 above the
unchanged 2,000-neuron ordinary-use reserve. The single guided trial is capped
at 200 neurons and 20 physical requests, with no extra model repair calls.
Stop if the cap/reserve binds; report an incomplete trial rather than increasing
the cap. Another full 42-case real-model gate is not funded by this allocation.
The existing real-model gate (37/42) remains historical; the offline/CI gates
will be rerun. No paid activation, production deployment or merge is authorized.

## Investigation

Required encounter/medication tools already run in the prior guided sample.
However, timeline-first planning gives shared evidence ids the `timeline` kind
and date/title permanently: graph merging appends structured text without
updating metadata. The evidence selector then treats those records as generic
timeline entries. Medication stops compete with old active orders and unrelated
background; two computed intervals compete without relevance to their window.

## Implementation and independent fixtures

Frozen implementation: `e412405`. No functional edits followed the live trial.
`merge_evidence` promotes timeline views to canonical structured metadata, copies
rather than mutates tool output, checks patient/resource/version identity and
unions injection flags. No planner change was needed: its required-tool floor
already covers encounters and medication history. A timeline-first integration
test verifies those tools still execute and the structured evidence survives.

For broad summaries only, the existing selector first includes an actual return
within the defined window and up to two dated non-active medication events,
then balances other evidence kinds within 16 whole entries and the existing
character budget. Focused lab/medication questions do not receive journey
priorities. The model is asked to compose a short cited claim for each selected
event, with a six-claim limit and the unchanged 1,000-token output budget. No
claim is inserted, repaired, replayed or silently discarded by the application.

Computed intervals already have a trusted tool grammar and strict verifier.
The prompt now shows a short projection only after the full tool statement
passes that unchanged verifier: both dates, their roles, gap and readmission
classification are retained; full source lineage remains in the evidence map.
Existing ISO/natural-date paraphrases were tested with independent dates. No
new paraphrase exception or numerical calculation was added to the verifier;
wrong gap, reversed roles and added treatment advice still fail.

The 19 added test cases use a separately authored synthetic journey with a
different patient, medication, dates and nine-day gap, including 40 distracting
active orders. They cover completeness, tool ordering, stale versions, foreign
identity, injection, non-readmission intervals, relevance, unchanged proof,
wrong computations and advice. Mocked outputs demonstrate that an omitted event
stays omitted. These are independent fixtures authored before implementation,
not independent clinical annotations or a separately reviewed human benchmark.

## Results — every prospective attempt retained

One live trial at `e412405`, 2026-10-18 03:39:47–03:40:07 UTC (client date
2026-10-17), Qwen3-30B-A3B on the existing free edge. The same scorer evaluates
all eight historical guided outputs from `90e9ac6` and every new attempt. No
new paired control, best-of selection, rerun or held-out tuning was performed.

| Measure | Previous eight guided runs | New eight attempts |
| --- | --- | --- |
| Completed LLM answers | 8 | 7; eighth composer blocked by budget |
| Accepted readmission mentions | 2/8 | 8/8, including one deterministic fallback |
| Accepted medication mentions | 0/8 | 8/8, including one deterministic fallback |
| Grounded typed return interval | 0/8 | 7/8 |
| Grounded medication stop/date/version | 0/8 | 8/8, including one deterministic fallback |
| LLM claims drafted / accepted / excluded | 48 / 38 / 10 | 42 / 42 / 0 |
| Deterministic fallbacks | 0/8 | **1/8**, caused by experiment cap |
| Composer JSON failures / citation-reference errors | 0 / 0 | 0 / 0 |
| Median / p95 attempt latency | 3.402 / 4.721 s | 2.483 / 3.802 s |

All **seven completed LLM answers** include both the 19-day return admission and
the dated medication stop, each verified against actual tool evidence. They
contain six claims apiece. Their median is 2.486 seconds (26.9% below the
historical sample); the all-attempt median includes the shorter blocked eighth
attempt, so it must not be interpreted alone. All 42 generated claims passed the
unchanged verifier; identity/version checks report zero citation errors. This
is finite synthetic evidence, not general semantic entailment or clinical
validation. The remaining claims still favor latest observations/background;
this experiment does not establish complete longitudinal trend coverage.

The cap stopped the eighth composer before an inference request: 164.15 neurons
had been charged/reserved, and its conservative maximum estimate would exceed
200. Fifteen physical requests were made (eight planners, seven composers),
with no repair calls. The harness exited **2** and marked the trial incomplete.
The existing fallback returned 15 verified deterministic claims; those are
retained but do not count as evidence of genuine LLM coverage. Across *all*
returned answers there are 57/57/0 claims, including that fallback.

Thus the at-least-seven/eight event-coverage criterion is met, but the
**zero-fallback and eight-completed-LLM-run confirmation are not met**. The
six-claim target holds for the seven LLM answers, not the fallback. No budget
increase or prompt retuning followed this result. Postflight usage was 5,909.2
of 8,000, leaving 2,090.8 and preserving the 2,000 reserve. Inference stops here.

## Regression gates and remaining limits

- Local Python: **409 passed** (previous 390), including all prior safety,
  isolation, verifier and composer regressions and 19 added cases.
- Frontend: **42 passed**, TypeScript and both builds pass. Worker: **25 passed**,
  typecheck passes. Real-backend Playwright: **30 passed** across three viewports.
- Repository lint, scoped formatter and seven-module mypy pass. Existing CI
  gates remain mandatory; the new evidence/scorer/tests are included in them.
- Fresh locked **offline** 42-case gate: **PASS, 41/42**, correctness 0.9565,
  critical safety 1.0, zero isolation/authorization/unsafe-output violations.
  The known `ho-013` lexical scoring miss remains; no case/threshold was changed.
- A new full **real-model** 42-case gate was not run: it cannot fit the declared
  free allocation/reserve. The previous **37/42** real-model report is historical
  and does not certify this frozen change. Broad model regressions remain
  unmeasured; the offline gate and mocks cannot substitute for them.
- Public edge/Space, main, verifier, safety thresholds, model artifacts and private
  configuration are unchanged. Merge/deployment still require owner approval.

Artifacts: [raw live attempts](../evaluation/results/guided_completeness_2026-10-17.json),
[all-attempt comparison](../evaluation/results/guided_completeness_comparison_2026-10-17.json),
[fresh offline gate](../evaluation/results/guided_completeness_gate_offline_2026-10-17.json).
The comparison records expected event annotations and scorer SHA; the raw report
records source hashes, every output, rejected claim, provider request and quota
snapshot. Expected medication names/dates are scoring annotations only; they are
never fed to inference. Rerunning requires a new declared allocation/output path.
