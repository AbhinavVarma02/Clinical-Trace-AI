# Project Status

**Updated:** 2026-11-07 (America/New_York) · **Stable:** `main` (2.0 public demo; last recorded release 2026-10-18) · **Beta development:** branch `feature/beta` (local and test-only; not deployed) · **Epic M1:** Draft PR #2, unmerged.

This page is the portable, version-controlled handoff. It contains no credentials, private Epic snapshot content or machine-specific evidence. Detailed private checkpoints stay in the git-ignored `.project-state/` folder of the working copy that produced them; a fresh clone does not have them. The Stable section further down is kept unchanged as the record of 2026-10-18.

## Current status (Beta channel, as of 2026-11-07 America/New_York)

| Stream | State |
|---|---|
| Stable 2.0 public demo | Merged to `main` (PR #1). Public edge https://clinical-trace-ai.vathadiabhinav.workers.dev with the HF Space co-host; the v1 dashboard is unchanged. Last recorded release 2026-10-18; not re-verified for this update. None of the Beta changes below are deployed. |
| Epic M1 sandbox adapter | Complete (all nine gates). Command-line only, read-only, Epic test patients only. Draft PR #2 stays unmerged ([EPIC_SANDBOX.md](EPIC_SANDBOX.md), [ADR 0008](ADR/0008-epic-sandbox-source-adapter.md)). |
| M2-S1 data-source registry | Committed and pushed on `feature/beta`; hosted CI green. Off by default; an Epic snapshot can be served locally as an isolated, deterministic-only extra source ([ADR 0009](ADR/0009-beta-channel-and-data-sources.md)). |
| M2.2-a discharge-summary match (P2) | Committed and pushed; hosted CI green. A note counts as a discharge summary by LOINC 18842-5 or its label in any letter case. |
| M2.2-b medication coverage provenance | Committed and pushed on `feature/beta`; hosted CI [42336192381](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42336192381) passed all five jobs at `94b9c5e`. |
| M2.2-c diagnostic result availability (ISSUE-3) | Committed and pushed on `feature/beta` at `9402eec`; [hosted CI 42339250764](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42339250764) passed all five jobs. Not deployed. Completeness, summary and review answers disclose unresolved report result references. M2.2-f adds bounded direct diagnostic handling. |
| M2.2-d medication vocabulary (L4) | Committed and pushed at `7d13776`; [CI 42387215792](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42387215792) passed all five jobs. Python CI: 948 passed, 3 skipped; evaluation 41/42. Prior local evidence: 951 passed, no skips. Production limited to planner/composer; safe abstention and drug-specific attribution. The original retrieval omission was deferred to M2.2-f. ISSUE-7 remains open. Stable unchanged; Beta not deployed. |
| M2.2-e encounter/note dates (ISSUE-5 residual) | Committed and pushed at `aef7aef`. [CI 42392508719](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42392508719) passed all five jobs. Hosted Python: 975 passed / 3 skipped; evaluation 41/42 with the known `ho-013` miss. Prior local gate: 168 focused and 978 full tests. Encounter and summary answers disclose existing linked-date findings once, with exact source versions, explicit note-date labels and required human review. Beta is not deployed. |
| M2.2-f retrieval and diagnostic relevance (ISSUE-6) | Implemented and verified locally, awaiting owner review. Bounded medication-class note recovery and direct diagnostic answers with exact evidence. No publication approval. |
| M2.3 controlled real-time Epic sandbox retrieval | **Mandatory approved target; not implemented.** Owner-only, read-only refresh of approved synthetic patients directly from Epic FHIR R4 through secure backend OAuth, followed by atomic validated snapshot promotion. Implementation, live calls, infrastructure and deployment each require separate owner approval. |
| ISSUE-7 | **Open, unresolved.** Records that declare no medication import coverage (the synthetic fixture) still cite the Patient for "no medication orders"; see [LIMITATIONS.md](LIMITATIONS.md). Changing it would alter approved synthetic answers and needs an explicit decision. |

The M2.2-c push/CI documentation was refreshed after commit `9402eec`; that commit itself records the earlier local verification state.

M2.2-d's pre-commit success/failure evidence below remains historical. Its approved normal push and hosted CI are complete; the status correction was included in M2.2-e commit `aef7aef`.

The mandatory M2.3 requirement supersedes the earlier static-snapshot-only Beta roadmap. Cached snapshots remain supported with explicit cache labels; public Epic data publication and public live refresh each require independent approval.

### Git baseline for this roadmap

| Ref | Commit |
|---|---|
| `main` (remote) | `72ae8c8`: Stable 2.0 (PR #1, merged) and dependency security fixes (PR #3, merged) |
| `feature/epic-sandbox-readonly` | `028e609`: Epic M1, Draft PR #2 into `main`, unmerged |
| `feature/beta` | `aef7aef36ae4b1abb7f48a81e631f0acb22fa215`, parent `7d13776517ee9a8db3332ec29d12bd188b4e696e`. The approved eight-file M2.2-e commit was pushed normally. [CI 42392508719](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42392508719) passed all five jobs. Hosted Python: 975 passed / 3 skipped; evaluation 41/42 with the known `ho-013` miss. Earlier M2.2-d and M2.2-c baselines are `7d13776` and `9402eec`. |

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

### M2.2-c behaviour (local, not deployed)

For each DiagnosticReport with unresolved result references, completeness checks append one medium-severity `diagnostic_result_unavailable` finding after all existing findings, preserving earlier check IDs. It lists the distinct unresolved references in their recorded order, cites the existing report/version and includes its clinical effective timestamp. Summary, missing-documentation and review answers disclose that those referenced results cannot be resolved, reviewed or interpreted from the imported record.

Only canonical Observation references in the same patient's source store resolve. Other reference forms are conservatively reported as unresolvable; no URL or identifier is guessed and no upstream request is made. A report with no nonempty result references creates no new finding. A present Observation is treated as available regardless of its value; this check performs no diagnostic interpretation. The report's references, conclusion and field-level provenance remain unchanged. The finding describes local import availability without asserting why a target is missing or whether it exists upstream.

Production changes are confined to `src/clinical/records.py`. The existing tools and composer carry the new finding without changes. Tests use invented records and MOCK fixtures only. M2.2-a, M2.2-b and unresolved ISSUE-7 retain their existing behaviour. M2.2-c left direct questions such as "What did the stress test show?" to M2.2-f. Changing clinical code changes the execution revision, so older interrupted checkpointed runs cannot resume.

### M2.2-d behaviour and prior local evidence (committed/pushed, not deployed)

Apixaban and torsemide now select medication tools by exact name. Class terms are separate from drug identity; the initial anticoagulant candidate list `{warfarin, apixaban}` is explicitly nonexhaustive. Blood-thinner phrases carry an ambiguity caveat. Class answers inspect only actual structured medication results and existing returned notes, including exact-name passages with BM25 zero. Administrative class terminology alone cannot establish medication use. Without supporting evidence the medication question abstains, preserving declared coverage and explaining the bounded vocabulary/retrieval limitation; an unreturned note is never evidence of source absence.

Mixed-drug events and doses require local drug-specific support. Negation, uncertainty, conditions, plans and imperatives do not become completed events. Conflicting statuses remain unresolved, and notes remain verbatim note text rather than verified orders/current use. Ambiguous multi-drug event questions and unsupported event or dose wording conservatively abstain. Structured order status/provenance and existing review/citation safeguards remain intact.

Class-note selection also retains sentence and section/title context: family-member medication history, allergies, explicit non-prescription/non-use and hypothetical or planned discussions cannot serve as patient-use confirmation. The three negative pre-commit examples abstain with zero claims; the CONTINUED apixaban control retains its verbatim quote, source/version and note-only caveats. Separate patient-medication sentences and actual stop/restart conflicts are preserved. The filter is deliberately conservative for ambiguous contexts.

| Local check | Verified result |
|---|---|
| Tests first, unchanged production | 85 failed / 21 passed; original 85 cases archived and preserved, with omission expectations revised under the owner's scope decision |
| Final focused / full offline tests | 344 / **951 passed** (796 existing + 155 new), no skips or expected failures; zero external attempts |
| Evaluation | **41/42**, known `ho-013`; all quality/retrieval metrics identical to fresh before and prior M2.2-c, latency measured separately |
| Synthetic / medication / original MOCK goldens and coverage matrix | Byte-identical before/final; no approved answer changes |
| Context safety before commit | Valid reviewed-source red: 18 failed / 5 passed; 37 additional synthetic/MOCK cases pass in the full gate; family/non-prescription/allergy abstain, CONTINUED quote preserved |
| Default API comparison | **0 differences**, 97 responses / 16,479 paths, versus fresh before and Epic M1 baseline; existing volatile exclusions only |
| Ruff, types and formatting | Pass: CI checks plus planner/composer/new-test mypy and new-test formatting; legacy formatting preserved |

**At the M2.2-d checkpoint, the ISSUE-6 omission still blocked Beta release acceptance.** With six administrative anticoagulation notes competing against an apixaban-only note, unchanged hybrid top-five retrieval omits the medication note. The corrected answer abstains with no administrative medication claim. Sparse returned apixaban evidence is quoted safely. The same omission occurs in a larger MOCK fixture; a sparse MOCK fixture verifies positive note provenance. See the exact [reproducer and boundary](LIMITATIONS.md). No retrieval query, `k=5`, ranking, tokenizer, index or tool architecture changed. No private snapshot or live service was accessed.

### M2.2-e behaviour and local verification (committed and pushed, not deployed)

The existing `encounter_date_conflict` detector is unchanged. The encounter tool carries only those patient-linked findings; the composer reuses their evidence in encounter and summary answers and avoids repeating findings already supplied by the inconsistency tool. It labels the note's own date separately from the admission/discharge date stated in its text and the encounter's recorded start/end. It quotes both values without deciding which is clinically correct. Source-version citations remain exact, and the review gate requires human review when the encounter tool returns a conflict.

A missing encounter counterpart remains a documentation gap, never a confirmed contradiction. The existing one-day tolerance, labelled valid ISO-date requirement and same-patient linked-encounter boundary remain unchanged. The fix does not add planner calls, change retrieval or alter the detector/verifier/registry/API schemas. Production scope: `src/agent/tools.py`, `src/agent/composer.py` and the small existing review-gate condition in `src/agent/graph.py`.

| Local check / history | Verified outcome |
|---|---|
| Tests first, production unchanged | 12 failed / 11 passed; zero external attempts. Failures reproduce missing encounter/summary disclosure and ambiguous note-date labels. |
| Preservation correction | Initial fix suppressed a follow-up plan elsewhere in a date-cited note. Existing regression reproduced 1 failure / 163 deselected; correction limits that suppression to the covered date excerpts. No original tests removed. |
| Focused / complete offline suite | 168 / **978 passed** (951 existing + 27 new); full suite has no skips/xfails. Final full suite includes the subsequent test-only typing correction. |
| Actual behaviour capture | 12 verified answers: six question forms each on invented and MOCK Epic data; one supported date finding each, exact citations and mandatory human review. Additional regressions cover multiple findings, missing/matching dates, tolerance, invalid/unlinked dates, other patients/sources, versions and discharge-only conflicts. |
| Evaluation / retrieval | Gate passed **41/42**, known `ho-013`; all non-latency quality/retrieval metrics identical to fresh before and M2.2-d. |
| Goldens / default API | Synthetic, medication, original MOCK and coverage outputs byte-identical; **0 differences across 97 responses / 16,479 paths** against fresh before and Epic M1, with unchanged volatile exclusions. |
| Static checks | Ruff, CI mypy (19 files), tools/composer/new-test mypy (3 files), both CI formatting scopes and new-test formatting passed. An expanded graph check reports the same pre-existing LangGraph `add_node` overload diagnostic as exact HEAD; no new diagnostic. |
| Retained nonpassing attempts | Private baseline driver initially expected a not-yet-created test file; sandbox API capture timed out and API-inclusive focused execution stalled. Guarded execution outside the sandbox passed. New fixture typing and intermediate Ruff issues were corrected; isolated mypy caches removed expanded-check cache contamination. These attempts remain history, not passing gates. |

Providers, tracing and dotenv are disabled; all clinical checks use invented/MOCK fixtures with zero recorded external attempts. Secret and protected-file receipts accompany the private review packet; private snapshots are excluded entirely, including hashing. The pre-existing expanded-graph typing diagnostic is a nonblocking final-polish item, with no unrelated production change. M2.2-e left ISSUE-6, ISSUE-7 and the mandatory M2.3 target open. The later owner-approved M2.2-e commit and push are recorded above. Stable is unchanged and Beta is not deployed.

### M2.2-f behaviour and offline verification

The agent first retrieves the usual five passages for medication-class questions. If they contain no eligible class note, it makes one additional patient-scoped search filtered to exact candidate names in note text before ranking. It keeps the same evidence limit, note context checks, source versions, import coverage and nonexhaustive vocabulary caveats. The raw retrieval benchmark and ordinary search defaults are unchanged. The crowded invented case now quotes the apixaban continuation, and the larger MOCK case resolves the note citation to its source.

A named diagnostic result question uses the existing completeness tool and imported report records. A matching report with unresolved results produces the existing local-availability disclosure with the report citation. Otherwise the agent quotes the recorded conclusion or cites up to five exact linked observation values with both report and observation versions. For an undated question with no matching report, an explicitly worded result in a relevant note can be quoted with its note date. Generic banners, unrelated tests and scheduled tests produce no result claim. Multiple matching reports require a date. This path is deterministic even when a provider is configured. The owner review found that a combined diagnostic and medication question could return only the diagnostic answer. The local correction asks for separate questions before collecting evidence when it identifies a standalone second clinical request. Medication mentions within diagnostic context still use the existing diagnostic route.

| Local gate | Outcome |
|---|---|
| Tests first | Initial 11 failed / 2 passed. Linked-value follow-up 1 failed / 12 passed, then corrected. |
| Original Python gate | 302 focused and 991 full offline tests passed. Invented and MOCK inputs. |
| Owner-review correction | Initial red 6 failed / 4 passed. A first guard caught two contextual temporal clauses, then the standalone-request check was tightened. 314 focused and 1003 full offline tests passed; locked evaluation, goldens and 97-response API comparison preserved. |
| Evaluation and retrieval | Gate passed 41/42 with known `ho-013`. Correctness 0.9565, outcome/tool/safety 1.0, unsupported claims 0.0125, isolation violations 0. The 19-query retrieval benchmark retained recall@5 0.9737, precision@5 0.3579, MRR@10 0.9474 and nDCG@10 0.951. |
| Goldens and API | Four original synthetic, medication, MOCK and coverage captures byte-identical. Zero differences across 97 default API responses / 16,479 paths against the frozen M2.2-e and Epic M1 baselines using the existing volatile-field rules. The guarded replay outside the sandbox succeeded after sandboxed TestClient timeouts. |
| Static and integrity | Ruff, CI and changed-file mypy, scoped formatting, secret scans and protected-file checks passed. Completed clinical runs recorded zero external attempts. |

Exact medication reproducer and remaining diagnostic limits are in [LIMITATIONS.md](LIMITATIONS.md#issue-6-bounded-medication-and-diagnostic-relevance). This is a local Beta change. ISSUE-7 and the M2.3 requirements retain their prior status.

### Test evidence

Local runs are offline and deterministic (no network, no language model, tracing off).

The regression counts and protected-file hashes below are retained M2.2-c evidence; they are not new measurements for the M2.3 roadmap.

| Check | Result |
|---|---|
| Tests first at M2.2-c | 17 expected failures / 143 passes on unchanged production code; 160 focused tests passed after the correction |
| Python suite at M2.2-c (local) | 796 passed (770 existing + 26 new), no skips or expected failures |
| Evaluation gate at M2.2-c (local) | Pass, 41/42 (the known `ho-013` miss); all quality and retrieval metrics identical to M2.2-b; latencies measured separately |
| Synthetic demo and medication answers | Byte-identical to the unchanged tree and M2.2-b (4 patients × 6 questions per golden, plus completeness findings) |
| Original resolved MOCK answers / medication coverage matrix | Byte-identical before and after; new dangling-reference MOCK tests verify the intended disclosures and report provenance |
| Default-mode API | 0 differences across 97 responses / 16,479 paths against both the unchanged tree and the Epic M1 baseline (`028e609`), using existing volatile-field exclusions |
| Lint, types, formatting, secret scan (local) | Clean |
| Protected-file integrity / evidence | All 99 hashes match, including 33 private files checked by hash only under restricted approval; source/test tree unchanged throughout the gate; zero recorded external attempts |
| Hosted CI on `feature/beta` | Latest recorded run at `aef7aef`. [CI 42392508719](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42392508719) passed all five jobs. Hosted Python: 975 passed / 3 skipped; evaluation 41/42 with the known `ho-013` miss. The logs record 52 successful steps and one expected failure-artifact-upload skip, frontend 53 unit / 36 browser tests, Edge 30 unit / 13 script tests and deploy dry-run, a 377-file secret scan, enforced dependency audits, and container build and smoke. The frontend install reported a high-severity warning outside the enforced production-only audit. Previous successful runs include 42387215792 (`7d13776`, M2.2-d, Python 948 passed / 3 skipped), 42342090985 (`4ac67fb`, M2.3 documentation), 42339250764 (`9402eec`, M2.2-c), and 42336192381 (`94b9c5e`, M2.2-b). |

### Remaining Beta milestones

Each step needs its own approval and lands as its own commit with its own CI.

1. **M2.2-c:** Implementation, offline gate, independent review, commit, push and hosted CI verification complete. This milestone added summary, completeness and review disclosure.
2. **M2.2-d:** implementation, offline verification, approved commit/push at `7d13776` and hosted CI 42387215792 complete. Initial bounded anticoagulant candidates are `{warfarin, apixaban}`; blood-thinner terminology is ambiguous and matching is nonexhaustive. The owner accepted the demonstrated top-five omission for this milestone only. Safe abstention is required; this is not approval to release Beta with the limitation unresolved.
3. **M2.2-e:** Local verification, approved commit and push at `aef7aef` complete. [CI 42392508719](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42392508719) passed all five jobs. Hosted Python: 975 passed / 3 skipped; evaluation 41/42 with the known `ho-013` miss. The linked source dates remain clinically unresolved.
4. **M2.2-f:** Local implementation and offline verification complete. Review the exact diff and behaviour evidence before any separately approved commit or push. ISSUE-7 and later Beta gates remain open.
5. **ISSUE-7:** owner decision on the synthetic Patient-cited gap.
6. **M2.3 (mandatory target):** controlled real-time, read-only Epic FHIR R4 developer-sandbox retrieval in a restricted owner-only Beta environment. Reuse M1 OAuth, client, mapping and provenance; add owner authentication/authorisation, approved-patient selection, refresh budgets, complete coverage states, atomic promotion/versioning and rollback. Implementation and each bounded live verification need separate approval.
7. **M2.4:** integrate owner refresh controls, source/cache status, retrieval times, resource counts, coverage and loading/error states into the Beta UI. D7 (UI approach) remains an owner decision.
8. **M2.5:** approve isolated Beta hosting/account placement and verify security using local MOCK services and synthetic fixtures. **M2.6:** separately approved restricted deployment, bounded owner-authorised live smoke and rollback verification. **M2.7:** release acceptance against the reliability, freshness, isolation and review gates. None is started or deployment authority.

Before release: A1 medication-date handling, medication import and a separately approved live gate with orders; documented Epic encounter-class mapping; multiple approved synthetic test patients; answer/UI coverage. These remain prerequisites, with no clinical dates or mappings invented. The incremental plan and approval dependencies are in [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md#beta-roadmap-mandatory-real-time-epic-sandbox-retrieval).

### M2.3 target architecture and acceptance

**Beta UI → Approved synthetic patient selection → Owner-authorized Epic refresh → Secure backend OAuth → Live Epic FHIR sandbox API → Atomic validated import → Existing Clinical-Trace evidence engine → Verified answers, citations and human review.**

"Real-time" means a fresh, bounded sandbox API retrieval requested by the owner. It does not mean continuously changing real-world clinical data, continuous polling or production hospital connectivity. The agent reads the promoted snapshot; it does not initiate Epic requests while answering.

- Authenticate the owner on the backend and authorise each refresh and patient selection. Demo persona headers, anonymous workspaces and an edge secret do not establish owner identity. No unrestricted public queries, arbitrary FHIR URLs or real patient identifiers.
- Keep OAuth signing keys and tokens on the protected backend; apply request, concurrency and rate budgets. Show source, MOCK/LIVE mode, snapshot version, last attempt outcome, last successful retrieval time, resource counts and import coverage. Retrieval time is never a clinical event time.
- Promote only a staged, validated, integrity-checked snapshot that satisfies the approved completeness policy. Pin answers, citations and reviews to its version; retain the prior version for explicit rollback. Failure leaves the prior snapshot intact and visibly cached; never silently substitute it or report it as a successful fresh response.
- Preserve distinct **not requested**, **not imported**, **returned none**, **failed** and **unresolved reference** states. None is proof of clinical absence. M1's medication coverage helper currently combines several states; the full M2.3 contract is missing.
- Keep minimal operational audit events without credentials or unnecessary clinical content. Separate all Beta compute, edge routes, storage, secrets, logs and release/rollback controls from Stable. Security/red-team tests target local MOCK services and synthetic fixtures only, never Epic infrastructure.
- No real hospital data, unrestricted public access or production Epic integration. Public live refresh, hosting, Cloudflare account selection and Epic data publication retain independent approval gates. Beta/Epic answers remain deterministic-only with external tracing off.

Reusable code and missing functionality are documented in [ARCHITECTURE.md](ARCHITECTURE.md#beta-m23-target-owner-only-live-sandbox-refresh); prerequisites, acceptance checks, risks and owner decisions are in [EPIC_SANDBOX.md](EPIC_SANDBOX.md#m23-mandatory-controlled-real-time-retrieval).

### Approval boundaries

- Approved product scope: the mandatory M2.3 requirement on `feature/beta`; prior M2-S1/M2.2-a/b/c work is complete. Beta/Epic sources remain deterministic-only. M2.3 implementation, live calls, infrastructure and deployment each require separate owner approval ([ADR 0009](ADR/0009-beta-channel-and-data-sources.md)).
- Implementation, offline verification, approved commits and pushes, and hosted CI verification are complete through M2.2-e. M2.2-f approval covers local investigation, implementation, offline verification and documentation. Earlier restricted M2.2-c integrity checks remain historical. The current task excludes all private snapshot access, including hashing.
- Deferred, not authorised: Beta backend host, Cloudflare account placement, public Epic content, Beta UI.
- Every commit, push, pull request, merge and deployment needs separate owner approval. So do live Epic or language-model calls, cloud resources, any UI change, replaying a private snapshot, and starting the next milestone.
- `main`, the Stable demo, production and Draft PR #2 stay unchanged.

**Development sequence:** M2.2-e is committed and pushed with hosted CI green. M2.2-f is locally verified and awaits owner review. Stable is unchanged and Beta is not deployed.

M2.3 is mandatory but not yet implemented; implementation, live calls, infrastructure and deployment require separate approvals.

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
