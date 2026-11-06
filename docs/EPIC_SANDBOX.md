# Epic on FHIR sandbox adapter (read-only, isolated)

This adapter connects Clinical-Trace AI to the **Epic on FHIR public R4 sandbox**, which contains only Epic-provided test patients. It is read-only and runs from a local command line. It is not production Epic connectivity, not an Epic certification, and not a clinical validation. The synthetic demonstration, the public deployment and the user interface do not use it.

Design decision: [ADR 0008](ADR/0008-epic-sandbox-source-adapter.md).

## Status

| Gate | What it proves | Status |
|---|---|---|
| Offline (MOCK) suite | Keys and JWKS tooling, token client, hardened client, mapping and provenance, isolated snapshots, unchanged agent/retrieval/verifier/review on an Epic-shaped fixture, API readiness | **Passing** (mocked transports and a hand-authored fixture only) |
| JWKS published and verified | Public key reachable over HTTPS, public members only, matches the local key | Not started: needs the owner's approval to generate the key and publish |
| G1 token (LIVE) | `private_key_jwt` accepted for the existing non-production client; granted scopes | Not run |
| G2 smoke (LIVE) | Patient.Read plus one search per registered API on one test patient | Not run |
| G3 import (LIVE) | One test patient mapped, validated, stored and verified in an isolated snapshot | Not run |

**Mock vs live.** A passing MOCK test is never reported as Epic access. LIVE results are recorded below only after the corresponding approved run, as sanitised counts, statuses and scopes.

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
| DocumentReference | `clinical-note` | yes; note text comes from `Binary` cited by the note |
| DiagnosticReport | none | yes |
| MedicationRequest | none | **no**, until A1 is approved (see Limitations) |

Which operations the sandbox actually accepts, and with which parameters, is unverified until G2.

Client behaviour:
- **Pages:** follows `next` links only on the sandbox base, up to 10 pages and 500 resources.
- **Retries:** 429/502/503/504 and timeouts are retried at most twice; `Retry-After` is honoured up to 10 s.
- **Token expiry:** a 401 triggers one token refresh.
- **OperationOutcome entries:** recorded by severity and code only.

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

## Limitations and pending decisions

- **A1 (needs approval):**
  - **What's wrong:** existing medication views use `meta.lastUpdated` as a medication date for version-1 orders (`src/retrieval/chunking.py`, `src/agent/tools.py`).
  - **Current handling:** MedicationRequest is not imported until this is fixed, so snapshots report "no medication orders", and the deterministic summary currently repeats that finding.
- **Discharge summary detection (P2 candidate):**
  - **What's wrong:** detection is an exact, case-sensitive match on "Discharge summary". An Epic-style "Discharge Summary" note is missed, producing a false "missing discharge summary" finding that the summary answer repeats.
  - **Current handling:** recorded as a strict expected failure in the compatibility tests.
- **Not mapped:**
  - RTF-only notes and `effectivePeriod` observations are rejected with issue codes.
  - Conditions without an ICD-10-CM code keep their text but carry no code, because existing views label the first code as ICD-10.
- **Readmission model:** disabled for this source. It was trained on a different population (ADR 0005).
- **What the tests show:** MOCK results show contract compatibility only, not Epic's actual resource shapes. G2 records the real shapes.

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
