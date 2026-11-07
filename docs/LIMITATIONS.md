# Limitations

## Clinical scope

- Educational prototype on synthetic data. Not a medical device, not clinically validated, not evaluated with clinicians.
- Outputs restate what the synthetic record documents; they are not clinical judgements.

## Data

- Four hand-designed synthetic patients (86 FHIR resources). They exercise specific behaviours (readmission, contradiction, incompleteness, versioned orders) and are not a representative population.
- FHIR support covers a validated subset of seven resource types; it is not a full R4 implementation. Patient ids use an underscore (`synthetic_p001`), which FHIR's id grammar does not allow; kept for compatibility with the v1 synthetic-id rule.
- The mock FHIR server and HTTP client have no SMART on FHIR or OAuth. A separate, command-line-only adapter reads Epic's public FHIR sandbox (Epic test patients only) with SMART Backend Services authentication ([EPIC_SANDBOX.md](EPIC_SANDBOX.md)). The application never calls Epic; a local server can serve an already-imported snapshot as an extra data source only when explicitly configured, and no deployment does so. No production EHR has been connected.
- M2.3 must add owner-only real-time retrieval of approved synthetic patients from Epic FHIR R4 through secure backend OAuth and atomic validated import. M2.3-1 was committed and pushed at `c4025d1`. [CI 42404572150](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42404572150) passed 5/5 with 1,000 Python passes, 3 skips and clinical evaluation 41/42. Stable is unchanged and Beta is undeployed. M2.3-2 provides owner authentication and bounded acquisition in an isolated local MOCK factory with an empty live allowlist. Serving stays unavailable. Full coverage, atomic runtime promotion, generation pinning and rollback remain future work. M2.4 adds UI refresh/status/provenance; isolated hosting/security/deployment/release follow in M2.5-M2.7. Cached snapshots must remain labelled and a fresh API response need not contain changed data. Public live refresh, hosting/account selection and Epic data publication require independent approval ([PROJECT_STATUS.md](PROJECT_STATUS.md)).

## Readmission model

- Trained on UCI Diabetes 130-US (1999–2008). Applying it to synthetic FHIR patients requires approximations (ICD-10 → broad ICD-9 categories, synthetic lab counts far below UCI norms, no procedures, unknown race). Outputs are out of distribution and uncalibrated for this population.
- Calibration/subgroup/error and historical distribution audits are implemented; scores substantially overestimate observed UCI event frequency (ECE 0.1901). No recalibration or retraining was applied. Sequence modeling remains an advanced extension.

## Agent

- Deterministic mode answers only intents covered by its rules; other questions use extractive quotes or abstain.
- LLM mode quality depends heavily on model size: `qwen2.5:1.5b` failed the quality gate; `qwen2.5:7b` passed but is ~180× slower than deterministic mode locally.
- Guards are pattern-based and can be evaded by unanticipated phrasing.
- Verification checks citations, numbers, computations, and lexical overlap. It does not prove semantic entailment: a claim that reuses the evidence's words with a different meaning could pass.
- The unreleased reliability update normalizes explicit dates, exact decimal formatting and a bounded set of unit spellings. Historical diagnosis/prescription exceptions require matching structured evidence and the correct event date. Other clinical paraphrases may still be rejected; thresholds have not been lowered. Development experiments and every retained candidate are documented in [AGENT_RELIABILITY.md](AGENT_RELIABILITY.md).
- Contradiction detection covers smoking status, lab values quoted in notes, and labelled ISO admission/discharge dates in a note that differ by more than one day from the linked encounter's recorded start/end. If the encounter records no matching start/end, the stated date is reported as incomplete encounter documentation, not a contradiction. Both values are quoted as recorded and never corrected.
- M2.2-e now surfaces those existing linked-date findings in encounter and summary answers as well as inconsistency/review answers, with explicit note-date labels, exact source versions and required human review. Committed and pushed at `aef7aef`. [CI 42392508719](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42392508719) passed all five jobs. Hosted Python: 975 passed / 3 skipped; evaluation 41/42 with the known `ho-013` miss. Beta is not deployed and the underlying ISSUE-5 dates remain clinically unresolved. No counterpart, dates within the existing one-day tolerance, unlabelled/non-ISO/invalid dates, unlinked notes and other patients' encounters do not become supported conflicts. The detector still only considers its first valid labelled date of each kind per note; broader date interpretation is outside this fix. A citation to a date excerpt does not hide a different follow-up section of the note. Tests-first failures, that preservation correction, final 978-test success, unchanged evaluation/goldens/API and retained environment/type-check attempts are recorded in [PROJECT_STATUS.md](PROJECT_STATUS.md#m22-e-behaviour-and-local-verification-committed-and-pushed-not-deployed). The expanded graph type check retains one pre-existing LangGraph overload diagnostic; CI-scoped checks pass. ISSUE-7 remains open. Stable is unchanged and Beta is not deployed.
- When a record has no medication orders, a medication question gets a note-only answer:
  - Note passages that mention medications are quoted verbatim, labelled as note text rather than verified orders.
  - An uncited note says that no orders are recorded and gives the import coverage the data source declared.
  - A question about a specific drug is answered only from a note entry that documents the asked event as done (past tense, not negated, conditional or planned, so "hold furosemide if …", "was not stopped" and "Stop furosemide." do not count); otherwise it abstains with an explanation.
  - A question about whether a drug is taken quotes that drug's documented status entry first (for example "STOPPED: furosemide 40 mg daily") and keeps its other mentions. A documented stop is labelled as note text, not verified current status, and a stop next to a restart or continuation is reported without choosing one. Only past-tense status words count, and only the retrieved passages are searched.
  - No reason, current status or dose is inferred.
  - M2.2-d is committed/pushed at `7d13776`, not deployed; CI [42387215792](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42387215792) passed all five jobs (Python 948 passed / 3 skipped; evaluation 41/42). Stable is unchanged; Beta is not deployed. M2.2-d deferred the retrieval omission to ISSUE-6. Exact apixaban/torsemide names now select medication handling; class terminology is separate from named drugs. Class matching is limited to the nonexhaustive `{warfarin, apixaban}` list and existing structured/returned evidence. Blood-thinner terminology is explicitly ambiguous. Other names, brands, abbreviations, spellings and classes remain deferred.
  - Drug events and amounts cannot be borrowed from another drug's local clause. Negated, uncertain, planned, conditional and imperative events remain unsupported. Unresolved multi-drug clauses, compound named-drug event questions and unsupported event or dose word order conservatively abstain; this remains a bounded pattern matcher, not general clinical-language understanding.
  - Administrative anticoagulation passages are not medication-use evidence. If no supported candidate is returned, the medication question abstains with coverage and nonexhaustive vocabulary/retrieval caveats. An unreturned drug mention is never treated as proof that it is absent from the source. M2.2-f adds one bounded class-note recovery query, described below.
  - Class-note selection rejects family-member history, allergy/sensitivity entries, explicit non-prescription/non-use and hypothetical or planned discussions as patient-use confirmation, considering both the whole sentence and its section/title. Those notes remain available in retrieved evidence but are not cited as medication-use proof. The requested family, non-prescription and allergy examples abstain; an affirmative CONTINUED entry remains a verbatim, caveated quote. Separate supported patient sentences and actual stopped/restarted statuses are retained. Mixed or ambiguous contexts may conservatively abstain; this is a bounded rule filter, not general semantic or clinical validation.
- The "no medication orders" completeness finding is made only when the data source has not declared that it left medication orders out of the import. When a source declares its medication import coverage (not imported, or searched with none returned), summary, missing-documentation and review answers state that coverage in uncited notes instead of a gap claim citing the Patient. A record that declares no coverage, such as the synthetic fixture, still reports the gap with a citation to its Patient resource, which does not itself show that orders are absent (tracked as ISSUE-7).
- M2.2-c (completed and pushed at `9402eec`, hosted CI 42339250764 green 5/5; not deployed) discloses DiagnosticReport result references that cannot be resolved in the same patient's source store. Completeness, summary and review answers cite the report/version and retain its clinical effective time while stating that the referenced results cannot be reviewed or interpreted from the imported record. The check reports local availability only: it does not infer a negative result, why a target is absent or whether it exists upstream. It does not fetch, interpret values, change report conclusions or resolve noncanonical reference forms by guessing. M2.2-f reuses this check for the supported direct diagnostic questions below. Tests use invented/MOCK data only, with no private snapshot clinical replay.
- Actual progress is streamed; clinical answer text is returned only after verification. Durable SQLite checkpoints support explicit compatible restart recovery. Cancellation is cooperative between nodes, not a forced interruption of running Python code.

## Retrieval

- Dense retrieval uses LSA (TF-IDF + SVD), not a neural encoder. No biomedical-embedding comparison has been run.
- Hybrid fusion lowered precision@5 relative to BM25 on the benchmark (see EVALUATION.md).
- Benchmark size is 19 queries.

### ISSUE-6: bounded medication and diagnostic relevance

**Medication class.** The original invented reproducer is retained in `tests/test_medication_vocabulary.py`. Its exact `vocab_runtime` also includes sparse, conflict and other-patient notes because the shared LSA fit affects rank.

1. Patient `synthetic_l4crowded` has six `Progress Notes`, ids `clinic-0` through `clinic-5`, dated `2026-09-01T09:00:00Z`, each saying `Anticoagulation clinic appointment was discussed; no medication list was provided.`
2. The seventh note, `crowded-apixaban`, has the same type/date and says `CONTINUED: apixaban 5 mg twice daily.`
3. For `Is the patient on an anticoagulant?`, the ordinary hybrid search at `k=5` still returns clinic notes 0, 4, 1, 5 and 2 (BM25 0.2043 each). At the M2.2-d checkpoint the agent abstained with no claims. M2.2-f recovers the continuation note through one additional same-patient note search and quotes it with exact source version and medication coverage caveats. The focused regression still asserts the original search omission.

The additional search runs when the first page lacks an eligible class note. It requires one of the existing exact candidate names in note text before ranking, uses the same result budget, and preserves the sentence and section context checks. Notes outside this bounded window and medications outside the vocabulary can still be missed. Family history, allergies, planned or negated use and administrative mentions remain insufficient to establish patient use. The note does not establish an active prescription or present-day status.

**Direct diagnostics.** The original extractive route could quote repeated sandbox banners because they shared the token `test`. The bounded route recognizes result questions about named stress tests, metabolic or diagnostic panels, chest X-rays, echocardiograms and ECG/EKG labels. It matches an imported report label exactly, with a year or ISO date when provided. Ambiguous matches require a report date. If the report lists an unresolved reference, the answer cites the local availability finding and avoids interpreting the missing result. It can otherwise quote a documented conclusion or display up to five linked observations with recorded values. An undated question with no matching report can quote a note sentence that names the test and states its result. This pattern-based rule does not cover all synonyms, dates, diagnostic concepts or note phrasing. When punctuation or a conjunction introduces a clearly separate medication or other clinical request, the agent asks for separate questions before retrieving evidence. Embedded medication context stays on the diagnostic route. The guard covers explicit request clauses. It can miss implicit or elliptical phrasing.

M2.2-f was committed and pushed at `505d7610c80da139e2977d15a42be3b45f0b9fdd`. Hosted Python recorded 1,000 passed / 3 skipped and clinical evaluation passed 41/42 (`ho-013` remains a known miss). Local verification used invented and MOCK data. Stable is unchanged and Beta is undeployed. ISSUE-7 and all remaining security and release prerequisites are open, including A1 medication clinical-date provenance. [CI 42400812536 passed 5/5](https://github.com/AbhinavVarma02/Clinical-Trace-AI/actions/runs/42400812536).

## Platform

- SQLite only; PostgreSQL and pgvector are not implemented.
- Demo personas are not authentication.
- Rate limits and caches are in-process and reset on restart; they do not coordinate across replicas.
- OpenTelemetry export is opt-in and local-file only; no network collector is configured. Operators must rotate trace files separately from SQLite retention.
- The handoff container passed real build/smoke tests in Actions 39074327485. Updated dependencies/runtime passed all four jobs in Actions 39077304115 at 6541d9b; inspect newer branch-tip CI separately. Local Docker became available on 2026-10-18; image size, cold start and memory are recorded in `evaluation/results/container_resources.json` (desktop CPU throttled with `--cpus`, an approximation of hosted vCPUs).
- The public edge's limits are free-tier platform limits (100,000 Worker requests/day, an 8,000-neuron inference cap). Under heavy use, live inference pauses until 00:00 UTC and agent runs are refused after 300 per day; both are disclosed in the UI.
- The public backend is co-hosted in the free Space ([PUBLIC_DEPLOYMENT.md](PUBLIC_DEPLOYMENT.md)); it sleeps after 48 idle hours and the full wake time is unmeasured. Live inference is relayed by the edge and works only on streamed requests; other requests answer in labelled deterministic mode.
- Free hosts provide no persistent disk for this setup: public runs, reviews and audit events live in an ephemeral SQLite file and disappear on restart or sleep.
- Public workspaces are random browser-held identifiers, not accounts. Anyone with the same browser storage sees the same workspace; clearing storage starts a new one.

## Evaluation

- Single-author synthetic cases; substring-based correctness; no human or model-based judging.
- Held-out cases are separate from development cases but not independent of the author.
- Live-LLM answers lose claims in verification: in a 48-run sample 19% of drafted claims were excluded, concentrated in the multi-encounter summary and warfarin questions, and the model's draft for the guided summary was unusable in about half the runs (deterministic fallback). Many exclusions are conservative false rejections (date and unit wording, past-tense "diagnosed/prescribed" record statements). Excluded claims and reasons are shown in the UI; see [UI_POLISH.md](UI_POLISH.md#verification-badge-follow-up-2026-10-19).
- That 48-run figure is historical. A fresh frozen-source control and subsequent development candidates are retained separately in [AGENT_RELIABILITY.md](AGENT_RELIABILITY.md). The control stopped at its predeclared resource cap; candidate samples are development evidence rather than independent clinical validation. The public application has not received these reliability changes.

- The focused guided-completeness follow-up has seven successful LLM answers,
  each covering both events, and an eighth cap-blocked deterministic fallback.
  It misses the zero-fallback confirmation target; no fresh complete real-model
  42-case gate was funded. See [GUIDED_COMPLETENESS.md](GUIDED_COMPLETENESS.md).
