# Epic on FHIR sandbox adapter (read-only, isolated)

This adapter connects Clinical-Trace AI to the **Epic on FHIR public R4 sandbox**, which contains only Epic-provided test patients. It is read-only and runs from a local command line. It is not production Epic connectivity, not an Epic certification, and not a clinical validation. The synthetic demonstration, the public deployment and the user interface do not use it.

Design decision: [ADR 0008](ADR/0008-epic-sandbox-source-adapter.md).

## Status

| Gate | What it proves | Status |
|---|---|---|
| Offline (MOCK) suite | Keys and JWKS tooling, token client, hardened client, mapping and provenance, isolated snapshots, unchanged agent/retrieval/verifier/review on an Epic-shaped fixture, API readiness | **Passing** (mocked transports and a hand-authored fixture only) |
| JWKS published and verified | Public key reachable over HTTPS, public members only, matches the local key | **Passed** 2026-11-06 |
| G1 token (LIVE) | `private_key_jwt` accepted for the existing non-production client; granted scopes | **Passed** 2026-11-06 (attempt 2) |
| G2 smoke (LIVE) | Patient.Read plus one search per registered API on one test patient | **Passed** 2026-11-06 |
| G3 import (LIVE) | One test patient mapped, validated, stored and verified in an isolated snapshot | **Passed** 2026-11-06 |

**Mock vs live.** A passing MOCK test is never reported as Epic access. LIVE results are recorded in [Evidence (LIVE)](#evidence-live) only after the corresponding approved run, as sanitised counts, statuses and scopes.

## Architecture and boundaries

```
Developer terminal ── python -m src.interop.epic.cli ...
  auth.py      RS384 client assertion -> sandbox token endpoint (token kept in memory only)
  client.py    read-only GETs pinned to https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/
  importer.py  one patient, one search per registered API category, cited notes only
  mapping.py   Epic resource -> existing FHIR contract (+ provenance), gated by validate_resource
  workspace.py var/epic_sandbox/<snapshot>/ -> unchanged build_runtime (model disabled)
               -> existing records, retrieval, LangGraph agent, verifier, review
```

**What the adapter never does:**
- The agent never calls Epic. Import is a separate, earlier step.
- Requests never leave the sandbox host. The FHIR base and token endpoint are code constants, redirects are refused, and proxy variables are ignored.
- Nothing in `api/`, `frontend/`, `deploy/` or the default runtime database (`var/clinical_trace.db`) is used or changed.
- Snapshots are never shown in the user interface or exposed through the API.

## One-time setup on the existing Epic app

The app **Clinical Trace AI** is already registered (Backend Systems, R4, SMART v1) and already has a **Non-Production Client ID**. Do not create a new app.

1. **Check the registration.** In fhir.epic.com → *My Apps → Clinical Trace AI*, confirm:
   - Backend Systems, with OAuth 2.0 enabled if the option is shown.
   - R4, SMART v1, unconstrained FHIR IDs.
   - A documentation URL over HTTPS.
   - A status of **Draft**. Epic shows Draft after *Save & Ready for Sandbox*, and changes can take up to about an hour to reach the sandbox.
2. **Check the Incoming APIs.** Keep only read and search APIs, and add `Binary.Read (Clinical Notes) (R4)` so note text can be read. Never add write APIs.
3. **Leave production alone.** Do not fill in the Production JWK Set URL and never mark the app *Ready for Production*.
4. **Pick test patients.** Choose one or two from Epic's official test-patient list. Never guess FHIR IDs.

## Signing key and JWK Set

The sandbox no longer accepts static public keys for backend OAuth, so the app needs a **Non-Production JWK Set URL**.

**Generate the key (owner-approved step):**

```bash
python -m src.interop.epic.cli keys generate
```

- This writes `<kid>.pem` and `<kid>.jwks.json` to `%USERPROFILE%\.clinical-trace\epic\keys\`. The tool refuses any path inside the repository and never overwrites a key.
- Set a passphrase in the session first (`EPIC_SANDBOX_KEY_PASSPHRASE`), or answer the prompt. Never put the passphrase in a file.
- `kid` is the RFC 7638 thumbprint, and the algorithm is RS384.
- Restrict the file to your account. On Windows: `icacls "<path>" /inheritance:r /grant:r "<your account>:F"`. The tool prints this with your account name filled in. Full control lets you delete the key during rotation.

**Publish only the public file:**
- Use a dedicated orphan branch `epic-jwks` containing `.nojekyll`, `jwks.json` and a short README.
- Enable GitHub Pages from that branch (Settings → Pages; enforce HTTPS).
- Optionally protect the branch against force-pushes.

**Verify, then register:**

```bash
python -m src.interop.epic.cli jwks lint jwks.json
```

```bash
python -m src.interop.epic.cli jwks check https://<github-user>.github.io/<repo>/jwks.json --key <path-to-kid.pem>
```

- `jwks check` confirms HTTPS, HTTP 200, no redirect, JSON content type, public members only and the RFC 7638 kid. It also confirms the published key matches the local key, and records `Cache-Control`, `Age` and `ETag`.
- Then paste the URL into the app's **Non-Production JWK Set URL**.

**Availability and caching:**
- GitHub Pages is a single point of failure for token requests. If it is unreachable, token requests fail with a categorised error; the public demo is unaffected.
- Pages controls its own caching (typically `max-age=600`). Epic's JWKS cache duration is not documented in the material reviewed. Plan for about 10 minutes of GitHub caching plus up to 60 minutes of Epic sync.

**Planned key rotation (each run needs approval):**
1. Generate K2.
2. `jwks merge` K1 and K2, then `jwks lint`, publish and `jwks check`.
3. Wait at least 24 hours (minimum 70 minutes).
4. Switch the local key path to K2 and run `live token-check`.
5. Keep K1 published for 24 hours.
6. Remove K1, then `jwks check` and `live token-check`.
7. Delete the K1 private file.

**If a key is compromised:**
1. Remove the key from `jwks.json` immediately, or clear the JWK Set URL in Epic.
2. Generate and publish a new key.
3. Wait for sync.

Already-issued tokens expire on their own (Epic's example lifetime is one hour). The adapter claims no revocation capability.

## Local configuration

Keep the configuration in `%USERPROFILE%\.clinical-trace\epic\epic.env`, outside the repository; it is never committed:

```
EPIC_SANDBOX_CLIENT_ID=<existing non-production client ID>
EPIC_SANDBOX_PRIVATE_KEY_PATH=<path to the .pem outside the repository>
EPIC_SANDBOX_JKU=<GitHub Pages JWK Set URL>
```

`EPIC_SANDBOX_ENABLED=1` must be set **in the session** for any LIVE command. Session variables override the file. A passphrase in the file is refused.

## Commands

| Command | Contacts | Output |
|---|---|---|
| `keys generate`, `jwks lint`, `jwks merge` | nothing | key id, file paths, lint result |
| `jwks check URL` | GitHub Pages only (unauthenticated) | check report |
| `live token-check [--preflight]` | Epic token endpoint (at most 1 token request) | granted scopes, lifetime |
| `live smoke --patient ID [--all-types]` | Epic sandbox (1 read + at most 9 searches + pages) | statuses, counts, field-presence shapes |
| `live import --patient ID [--max-notes 10]` | Epic sandbox (1 read + 7 searches + at most 10 notes) | new snapshot id, mapping counts |
| `workspace import-fixture FILE --key NAME` | nothing | MOCK snapshot |
| `workspace list`, `show`, `verify` | nothing | manifest summary; re-map and hash checks |
| `workspace review SNAPSHOT --question TEXT` | nothing (deterministic, 0 LLM calls) | outcome, verification, citation provenance counts; the answer only with `--show-answer` |

Responsible use:
- Run LIVE calls in a short window for one patient at a time, and never loop over sandbox records.
- Avoid Sunday evenings US Central time, when the sandbox refreshes.

Sanitised LIVE reports are written under `var/epic_sandbox/reports/` (git-ignored).

## Supported operations

Searches mirror the registered Incoming APIs. All are patient-scoped and read-only:

| Resource | Search category | Imported |
|---|---|---|
| Patient | read | yes |
| Encounter | none | yes |
| Condition | `problem-list-item`, `encounter-diagnosis` | yes |
| Observation | `laboratory`, `vital-signs` | yes |
| DocumentReference | `clinical-note` | yes; note text comes from `Binary` cited by the note, for at most `--max-notes` notes (default 10) chosen as described in [Note selection and coverage](#note-selection-and-coverage) |
| DiagnosticReport | none | yes |
| MedicationRequest | none | **no**, until A1 is approved (see Limitations) |

G2 confirmed that the sandbox accepts every operation above with these parameters, for one test patient (see [Evidence (LIVE)](#evidence-live)).

Client behaviour:
- **Pages:** follows `next` links only on the sandbox base, up to 10 pages and 500 resources.
- **Retries:** 429/502/503/504 and timeouts are retried at most twice; `Retry-After` is honoured up to 10 s.
- **Token expiry:** a 401 triggers one token refresh.
- **OperationOutcome entries:** recorded by severity and code only.

## Note selection and coverage

`live import` fetches at most `--max-notes` note bodies (default 10, maximum 25), one `Binary` read each. Which notes are fetched follows policy `epic-note-select/1` in `importer.select_notes`:

1. **Eligible notes:**
   - not `entered-in-error`;
   - an HTML or plain-text attachment by URL;
   - a full clinical `date`.

   No request is spent on other notes; mapping would reject them anyway.
2. **Note type:** the LOINC code decides the type: Discharge summary 18842-5, History and physical 34117-2, Consult 11488-4, Progress note 11506-3, Procedure note 28570-0. If there is no LOINC code, the type text decides, ignoring case and punctuation. On a conflict LOINC wins, and the conflict is counted.
3. **Key notes first:** the best Discharge summary, History and physical and Consult note are reserved first, in that order. "Best" means `current` and `final`/`amended` first, then newest, then lowest Epic id.
4. **Remaining slots:** the newest other eligible notes fill the rest. Dates are compared as UTC instants. Equal times go to the higher-priority type, then the lower Epic id.
5. **Stable result:** input order never changes the result.
6. **Bounded requests:**
   - A failed fetch still uses its slot, so requests stay bounded.
   - A `Binary` cited by two selected notes is read once.

The policy only chooses notes. It never changes a note's clinical `date` or its encounter reference.

**Why.** The public sandbox is shared, so recent test notes from other developers outnumber a patient's original notes. The first LIVE import (G3) kept only the newest 10 notes by date. It therefore left out the patient's Discharge Summary, Consult and H&P.

**Coverage report.** `workspace show` and `workspace review` print `note_coverage`. It is computed offline from the snapshot's raw records, `Binary` index and bundle, so it also works for snapshots written before this policy. It counts notes by type and status:

| Status | Meaning |
|---|---|
| `imported` | mapped into the snapshot |
| `excluded_by_cap` | eligible, but beyond the note cap |
| `body_unavailable` | no HTML/plain-text body, the fetch failed, or the fetched body was empty, too large or unreadable |
| `not_eligible` | `entered-in-error`, or no full clinical date |
| `not_fetched_unattributed` | an older snapshot that did not record its selection and had failed fetches |

The report also includes:
- the key note types present in Epic but not imported;
- each imported note's type, date and period:
  - **historical:** more than 365 days before the newest clinical note date in the record (retrieval time is never used);
  - **recent:** otherwise;
- whether each imported note kept its original date and encounter link.

A one-line summary is added to the snapshot's scope notes, for example:

> Clinical notes: 10 of 53 imported (selection policy not recorded); 43 excluded by the import cap (Discharge summary 1, History and physical 1, Consult 1, Progress note 37, Procedure note 3). Key note types present in Epic but not imported: Discharge summary, History and physical, Consult. Answers cover imported notes only; each note keeps its original clinical date.

**Limits.**
- Answers cover imported notes only. The agent's answer text does not repeat the coverage report; changing that would mean an agent change, which needs separate approval.
- The snapshot imported at G3 (`20261106t140916-09358b35`) is immutable and keeps its original 10 notes. Only a new, separately approved LIVE import would apply this policy to Epic data.

## Provenance and field status

Every mapped resource passes the unchanged `validate_resource` and carries the extension `urn:clinical-trace:epic:source`, which holds:
- `sourceReference`: the original `Type/id`;
- `sourceVersionId` and `sourceLastUpdated`, when Epic supplied them;
- `sourceContentSha256`;
- `retrievedAt`, `snapshotId` and `mappingVersion`.

Every source field is accounted for with one of these statuses:

| Status | Meaning | Example |
|---|---|---|
| original | copied verbatim | `status`, `period` |
| transformed | format or order changed, same meaning | ICD-10 / LOINC / HL7 category listed first; HTML note → plain text; partial onset moved to `onsetString`; references rewritten |
| substituted | value taken from a *different field of the same resource* | `class.display<-class.code`; `medicationCodeableConcept.text<-medicationReference.display`; `meta.lastUpdated<-retrievedAt` (record metadata only) |
| unavailable | a contract-relevant value Epic did not provide | `meta.lastUpdated`; `onsetDateTime` when only a year is known |
| unsupported | present in Epic but not carried | `identifier`, `telecom`, `address`, `serviceProvider` |

**Identity:**
- Patients become `synthetic_epic<12 hex>` and other resources `epic-<type>-<16 hex>`. These are deterministic SHA-256 namespacing of the source id.
- `meta.source` is the absolute Epic URL, including `_history/<v>` when Epic supplied a version.
- Tags mark each record as an Epic sandbox test record.

**Clinical time:**
- Clinical times come only from the source field with the same meaning.
- Retrieval time is never used as a clinical time.
- If a required clinical time is missing, partial or a period (`effectivePeriod`), the resource is **rejected and counted**, never filled in.
- Every resource is version 1 of an immutable snapshot; Epic's version is kept in provenance only.

**Re-audit:** `workspace verify` re-maps the raw records kept in the snapshot and checks the bundle, the database and every provenance hash.

## Isolation

- **Patient binding:** every searched resource must reference the requested patient (relative, or absolute on the sandbox base). Otherwise the import aborts and nothing is written.
- **Notes:** `Binary` is read only for references cited by that patient's verified notes.
- **One patient per snapshot:** each snapshot has its own database and holds exactly one patient; this is checked after ingestion.
- **Storage location:** paths outside `var/epic_sandbox/` are refused, and so is the application's runtime database.
- **Existing safeguards still apply:** the policy, retrieval-partition, tool and verifier isolation checks run unchanged. CLI personas are in-memory and scoped to the snapshot's patient.

## Evidence (MOCK)

Offline tests use `httpx.MockTransport` and a hand-authored fixture (`tests/fixtures/epic_sandbox/`). The fixture is shaped like Epic responses but is not a recorded Epic response.

| Test file | Covers |
|---|---|
| `tests/test_epic_keys.py` | key generation and refusal inside the repo, RFC 7638 kid (RFC test vector), JWKS lint, published check, rotation set |
| `tests/test_epic_auth.py` | assertion claims and signature, form POST, caching and refresh, error categories without leaks, local config |
| `tests/test_epic_client.py` | the Phase 1 package scenarios plus refresh, throttling, OperationOutcome, Binary scope, URL pinning |
| `tests/test_epic_mapping.py` | validator conformance, field-status completeness, provenance, clinical-time rule, no invented values |
| `tests/test_epic_workspace.py` | isolation, immutability, tamper detection, re-map verification, bounded import |
| `tests/test_epic_agent_compat.py` | all 11 tools and 6 questions on the unchanged agent; citations resolve to Epic sources; hash-bound review |
| `tests/test_epic_m2_readiness.py` | existing `/api/v2` routes over a snapshot (test-only patching) |
| `tests/test_epic_telemetry_cli.py` | spans, counters and reports contain no tokens, identifiers or text; LIVE commands refuse without opt-in |
| `tests/test_epic_cli_offline.py` | the CLI loads no repository `.env`, keeps LangSmith tracing off (no outbound requests), and refuses to review if it cannot stay offline |
| `tests/test_epic_note_selection.py` | note selection policy: key types first, determinism, ties, LOINC/text, ineligible notes, preliminary notes, UTC ordering, small caps. Also: bounded `Binary` reads; original dates and encounters kept; the coverage statuses; historical notes; the discharge question citing the Discharge summary; the 2019 H&P cited with its 2019 date |

## Evidence (LIVE)

Each run below was approved separately by the owner and run from the owner's terminal; the owner typed the key passphrase. Sanitised reports are kept locally under `var/epic_sandbox/` (git-ignored). Scope: the Epic on FHIR **public sandbox** and one Epic-provided test patient. This is not production connectivity, an Epic certification or a clinical validation.

**JWKS (2026-11-06).** `jwks check` passed on the GitHub Pages JWK Set (`epic-jwks` branch): HTTP 200, `application/json`, `Cache-Control: max-age=600`, the expected kid present, public members only, lint clean, and the published key matches the local key.

**G1 token check (2026-11-06).**
- **Attempt 1:** refused with `invalid_client`. The app configuration had not yet been saved with *Save & Ready for Sandbox*. The JWKS preflight passed, and the local clock matched Epic's.
- **Attempt 2:** passed after the owner saved the configuration and Epic synced it. 1 token request, `expires_in` 3600 s.
- **Granted scopes:** `system/Patient.read`, `system/Encounter.read`, `system/Condition.read`, `system/Observation.read`, `system/MedicationRequest.read`, `system/DocumentReference.read`, `system/DiagnosticReport.read`, `system/Binary.read`.
- **Requests used:** 2 of the 3 G1 token requests budgeted.

**G2 smoke (2026-11-06).**
- **Test patient:** Warren McGinnis, FHIR ID `e0w0LEDCYtfckT6N.CkJKCw3`, from Epic's official *Sandbox Test Data* page.
- **Requests:** 10 (1 token, Patient.Read, 8 searches, one page each). Every operation returned `ok`.

| Operation | Count | Notes |
|---|---|---|
| Patient.Read | 1 | |
| Encounter | 1 | `class.system` is not HL7 v3-ActCode |
| Condition `problem-list-item` | 2 | |
| Condition `encounter-diagnosis` | 0 | OperationOutcome `warning:4101` (no results) |
| Observation `laboratory` | 1 | `effectiveDateTime`, `valueQuantity` |
| Observation `vital-signs` | 73 | all `effectiveDateTime`; 59 `valueQuantity`, 14 `component` |
| MedicationRequest | 0 | OperationOutcome `warning:4101` (no results) |
| DocumentReference `clinical-note` | 53 | 2 attachments each, all by URL (`Binary`) |
| DiagnosticReport | 2 | `effectiveDateTime` |

**Observed resource shapes:**
- `meta.versionId` was absent on every resource.
- `meta.lastUpdated` was present on Encounter, Condition, the laboratory Observation and DiagnosticReport. It was absent on Patient, the vital-sign Observations and DocumentReference.
- The smoke report reduces attachment content types to `other`, because its label filter rejects `/`. G3 recorded the actual note formats.

**G3 import (2026-11-06).**
- **Command:** `live import --max-notes 10` on the same test patient.
- **Requests:** 19 (1 token, Patient.Read, 7 searches, 10 `Binary` reads). MedicationRequest was skipped (A1). The fetch was complete.
- **Notes:** all 10 note bodies fetched were `text/html`, with 0 failures. All 53 notes offered an HTML or plain-text attachment.
- **Mapped:** Patient 1, Encounter 1, Condition 2, Observation 74, DiagnosticReport 2, DocumentReference 10.
- **Rejected:** `DocumentReference:note-content-unavailable` 43. These are the notes beyond the 10-note cap, whose bodies were not fetched.
- **Field status:** transformed 464, unsupported 222, substituted 84, unavailable 84.
- **Dangling references:** 1. A `DiagnosticReport.result` points to an Observation that the `laboratory` search did not return.
- **`workspace verify`:** passed. It checked 90 resources: the bundle matches a fresh re-map, there are 0 provenance, binary or store mismatches, and the patient is isolated.

## Limitations and pending decisions

- **A1 (needs approval):**
  - **What's wrong:** existing medication views use `meta.lastUpdated` as a medication date for version-1 orders (`src/retrieval/chunking.py`, `src/agent/tools.py`).
  - **Current handling:** MedicationRequest is not imported until this is fixed, so snapshots report "no medication orders", and the deterministic summary currently repeats that finding.
  - **How answers disclose it:** the snapshot manifest says medication import was off, and answers add the note "Structured medication orders were not imported from the data source for this record…". Medication answers quote note mentions as note text, not verified orders. The Patient is never cited as proof that orders are absent.
- **Discharge summary detection (P2 candidate):**
  - **What's wrong:** detection is an exact, case-sensitive match on "Discharge summary". An Epic-style "Discharge Summary" note is missed, producing a false "missing discharge summary" finding that the summary answer repeats.
  - **Current handling:** recorded as a strict expected failure in the compatibility tests.
- **Not mapped:**
  - RTF-only notes and `effectivePeriod` observations are rejected with issue codes.
  - Conditions without an ICD-10-CM code keep their text but carry no code, because existing views label the first code as ICD-10.
- **Readmission model:** disabled for this source. It was trained on a different population (ADR 0005).
- **What the tests show:** MOCK results show contract compatibility only, not Epic's actual resource shapes. G2 and G3 recorded the real shapes for one test patient (see [Evidence (LIVE)](#evidence-live)). Other test patients may differ.

## Future work (each needs separate approval)

- **M2-S1, local source switch** (about 25 backend lines, off by default, local dev server only; no UI, edge or Space change):
  - `get_runtime()` selects a snapshot via `CLINICAL_TRACE_DATA_SOURCE=epic-sandbox-workspace`.
  - Persona panels come from the snapshot manifest.
  - `/system/status` reports the data origin truthfully.
  - Guided cases reference synthetic ids and would not apply.
- **M3, public exposure:** only a static, pre-imported snapshot, and never keys or live Epic calls on public infrastructure.

## Rollback

- **Disable:** unset `EPIC_SANDBOX_ENABLED`.
- **Revoke:**
  - Clear the JWK Set URL in Epic.
  - Disable Pages or delete `epic-jwks`.
  - Delete the local key.
- **Remove data:** delete `var/epic_sandbox/`.
- **Revert code:** revert the feature-branch commits. Nothing in the running application imports `src/interop/`.

## Review of the earlier Phase 1 package

- **Kept:**
  - host and base-path pinning, including pagination links;
  - no redirects; `trust_env=False`;
  - streamed size cap; loop and page limits;
  - subject binding; sanitised errors;
  - its 12 mock scenarios, ported and re-run.
- **Fixed:**
  - the pasted static token was replaced by the backend JWT flow, with refresh on 401;
  - long Epic ids are accepted;
  - OperationOutcome search entries are handled;
  - a total cap replaces the per-page `_count` check;
  - category searches were added;
  - throttling and retries, Content-Type checks, absolute subject references and Binary support were added;
  - telemetry was added;
  - the module moved out of `src/clinical/` so the agent's execution revision is unchanged.
- **Not adopted:**
  - the token-pasting smoke script;
  - the earlier documentation, which predates the sandbox's JWK Set URL requirement;
  - the copy-into-repo instructions.
