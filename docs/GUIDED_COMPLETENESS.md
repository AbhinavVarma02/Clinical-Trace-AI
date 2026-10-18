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

## Results

Pending implementation and verification. All outcomes will be retained here.
