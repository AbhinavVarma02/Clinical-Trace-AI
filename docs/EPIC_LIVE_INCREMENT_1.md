# Epic LIVE acquisition — Increment 1

Local implementation, November 8, 2026 (America/New_York). Stop for owner review.
No real Epic requests, credential/config/key-file access, dependency installation,
staging, commit, push, merge or deployment occurred. Stable and browser routing are
unchanged. The default LIVE allowlist (an explicit, immutable `LiveAllowlist`; the former `beta_catalog.LIVE_PATIENTS`
global was removed) is empty.

## Backend contract

`src/interop/epic/beta_live_acquisition.py` adds `LiveAcquisition`, reusing the M2.3
acquisition engine, OAuth assertion/token parser, pinned FHIR URL and patient
checks, mapper, source validation, coverage and content-free catalog.

Construction requires all of the following before signing or transport creation:

- Explicit `enabled=True` and the existing `EPIC_SANDBOX_ENABLED=1` gate.
- A server-controlled alias in the explicit, immutable `LiveAllowlist` (at most one approved
  patient; empty by default). MOCK aliases/IDs and malformed IDs are refused when it is built. There is no patient/query input from a browser.
- A running catalog attempt bound to that alias and `live-initial-r4-v1` policy.
- A current authorization callback; owner/patient revocation and removal from the
  allowlist are checked throughout acquisition and normalization.
- An already available `EpicConfig` and RSA key supplied by the trusted backend
  caller. The adapter does not load configuration or unlock/read keys.

The caller retains the existing orchestration responsibility: admit with the
`Limits` cooldown and hourly quotas and LIVE policy, start the attempt, await
`run()`, and finish the catalog attempt with its categorical result/operation
counts. This increment adds no worker, HTTP route, scheduling or startup wiring.
`candidate` contains a validated manifest and normalized bundle in memory on
success; failure/cancellation clears it. Tokens and retained upstream resources
are discarded on exit. Each adapter instance runs once.

The fixed operation allowlist is exactly:

| Resource | Operation/filter |
|---|---|
| Patient | Read the approved ID |
| Encounter | Search by approved patient |
| Condition | Patient + problem-list-item |
| Condition | Patient + encounter-diagnosis |
| Observation | Patient + laboratory |
| Observation | Patient + vital-signs |
| DocumentReference | Patient + clinical-note; metadata is extracted locally (no request-time projection) |
| DiagnosticReport | Search by approved patient |

The token must grant all six required resource read scopes. The historical M1
registration's additional `system/Binary.read` and `system/MedicationRequest.read`
grants may be present; they never authorize dispatch. Wildcard, write, unknown and
missing required grants fail closed. No Binary, MedicationRequest, individual
document read, attachment fetch, arbitrary search or write operation is enabled.
Note attachment URLs, inline bodies and narrative are excluded from the candidate. Epic's
search response may still contain attachment URLs; they are received transiently, never
retained in the candidate and never followed.

## Transport and evidence boundaries

Only the existing fixed Epic sandbox HTTPS FHIR R4 base and token URL are used.
The owned asynchronous transport verifies TLS, ignores ambient proxy/CA overrides,
has no redirects or transport retries, and uses one connection. The shared
acquisition layer counts every outbound request before dispatch, including OAuth,
pages, eligible retries and the single token renewal.

Existing maximum budgets remain: 48 requests/attempt, 240/hour, 90 seconds total,
5 seconds/request, one eligible retry/request, one 401 renewal/attempt, four pages
and 200 matches/search, 1,200 matches/attempt, 2,000,000 bytes/FHIR response,
64 KiB/token response and 16 MiB/attempt. Overrides may only tighten limits.
Response and transport cleanup are also time-bounded. Encoded/non-JSON responses,
duplicate JSON keys, incomplete bundles and conflicting resource identities fail
closed. Normalized evidence is additionally capped at 20 MiB.

Continuation links must retain the identical resource path, patient and category
filters. Exactly one bounded `page` or `_cursor` is accepted;
duplicate/extra filters, foreign endpoints, loops, unknown server-session formats
and links without patient filters are refused. Alternate query ordering/encoding
is canonicalized. Returned resources and nested patient references are checked.

The existing mapper preserves clinical dates, source versions and content hashes.
Manifest provenance now follows the acquisition's actual source/mode/policy.
An injected in-process MockTransport produces MOCK provenance; the production
transport path produces LIVE provenance. Fake transport tests are never evidence
of actual Epic access. DocumentReference coverage explicitly excludes note bodies.

The existing generation store rejects every `LiveAcquisition`, including one using
an injected MockTransport, before creating staging files or changing pointers.
Its existing MOCK manifest, generation selection, pinning, rollback and browser
behavior remain covered by the focused regressions.

## Files changed in this increment

- `src/interop/epic/beta_live_acquisition.py` — new backend adapter.
- `src/interop/epic/beta_acquisition.py` — reusable transport/policy hooks; preserved
  MOCK entry point and constructor guard; bounded response cleanup.
- `src/interop/epic/beta_catalog.py` — distinct optional LIVE attempt policy;
  existing admission defaults and empty LIVE patient allowlist retained.
- `src/interop/epic/beta_staging.py` — source/mode/policy-aware manifests.
- `src/interop/epic/beta_generations.py` — explicit LIVE publication denial.
- `tests/test_beta_live_acquisition.py` — synthetic fixtures and fake transports.
- `docs/EPIC_LIVE_INCREMENT_1.md` — this contract and completion record.

All six M2.4-1A implementation/test files and `.git/index` were verified against
their starting SHA-256 hashes and are unchanged. Other pre-existing work remains.

## Verification actually executed

Final native offline pytest run: **115 passed, 0 failed/errors/skips, 9.27 s**.
One existing Starlette/httpx deprecation warning. **Zero unexpected guarded socket
events.** Used the existing `tests.offline_network.install()` guard before imports,
disabled pytest plugin autoload for this process, a unique ignored scratch
directory, and a 50-second stack-dump/exit limit. No private runner was invoked,
repaired or rebuilt.

The final selection comprised all **69 new adapter cases**, plus **35 existing
owner-refresh cases** and **11 existing generation cases**. Existing test functions:

- Owner refresh: `test_exact_profile_two_patients_and_no_publication`,
  `test_valid_pagination_and_empty_terminal_search`,
  `test_pagination_scope_checks_precede_dispatch`, `test_response_retry_budgets`,
  `test_renewal_is_once_for_entire_refresh`,
  `test_absolute_deadline_caps_slow_stream`,
  `test_transport_rejects_unapproved_operations_before_token`,
  `test_retry_after_outside_absolute_budget_never_dispatches_a_retry`,
  `test_unauthorized_renewal_does_not_reset_network_retry_budget`,
  `test_retry_never_uses_expired_bearer`.
- Generations: `test_publication_both_patients_metadata_coverage_and_lineage`,
  `test_failed_candidate_never_replaces_previous_generation`,
  `test_pinned_run_and_review_survive_refresh_and_restart`,
  `test_explicit_cache_stale_rollback_and_age_limits`.

Local JUnit receipt (ignored):
`.project-state/live-increment-1-final-d5c73a3c9dc34835ba53603cfa1ceeeb.xml`.
Earlier development runs passed 59 adapter cases, then 114 focused cases before
the final transport-close deadline test/fix. Ruff check and format check passed
for all six changed Python files; `git diff --check` passed.

Initial sandbox and elevated attempts using the existing `.pytest_run_tmp` parent
each produced **1 pass and 59 fixture setup errors**, specifically Windows access
denied creating temporary directories. A sandbox run in writable ignored scratch
passed its first 13 synchronous cases and stalled at the first asynchronous case;
only its identified processes were stopped. The approved native runs above used
the writable scratch directory and completed. These are scoped observations,
not a repair or acceptance of the retired verification system.

Not executed: full Python suite, fresh default API comparison, browser/frontend
checks, hosted CI, real Epic OAuth/FHIR/TLS/pagination or clinical evaluation. The
prior Phase A result is historical. M2.4-1A acceptance, DT-TOOL-005 and the previous
30 unclassified collection errors remain unresolved.

## Deferred work and smallest Increment 2

No observed Increment 1 code blocker remains in the focused offline checks. Actual
Epic continuation formats, granted scopes, metadata projection, completeness and
capacity remain unverified. The empty approved-patient list deliberately prevents
LIVE execution. There is no LIVE publication or sandbox-to-browser acceptance.

Track existing non-blocking issues for later: M2.4-1A verification/acceptance,
Windows tooling, ho-013, ISSUE-7, A1 medication-date evidence, note completeness,
revocation cleanup/retention, independent review and host/storage/key-custody gates.
This change does not resolve or expand those workstreams.

Smallest recommended Increment 2: separately authorize one bounded **backend-only
functional sandbox check** for one explicitly approved official synthetic patient
using the existing non-production registration. Confirm actual grants, metadata
projection, continuation shape and normalized coverage, with publication still
disabled. Stop for review before owner-route/generation/browser wiring. No part of
Increment 2 has been started or authorized by this implementation.

## Addendum — November 9, 2026: Epic 4101 empty-search compatibility fix

Reading the adapter against the November 6 M1 evidence showed Epic answers an empty search with a
search-mode `outcome` entry containing a warning-only `OperationOutcome` (code 4101, seen for
`Condition:encounter-diagnosis` and `MedicationRequest`). The M1 client skipped such entries, but
`LiveAcquisition` rejected them as `invalid_bundle` and would have stopped at the fourth operation.
An offline reproduction confirmed this before the fix.

`beta_live_acquisition.py` now treats an entry as "not a match" only when its search mode is
`outcome`, its resource is an `OperationOutcome` and every issue severity is `warning` or
`information`. Error/fatal outcomes, an outcome presented as a match, a non-outcome resource and
extra entry keys still fail `invalid_bundle`. Only short fixed-format codes are retained in
`outcome_codes`; outcome text is never kept. The shared MOCK acquisition is unchanged.
One parametrized test (six cases) was added; the adapter test file now has 75 cases.

Verification (offline, guarded native run): adapter + owner-refresh + generation test files with
the five process-kill/recovery cases deselected: **231 passed, 0 failed/skipped**. This supersedes the
115-pass focused result above as the current focused checkpoint but is a different, wider selection and
is **not full acceptance**. Real Epic behavior remains unverified: the single owner-run bounded sandbox
attempt is still pending. No real request, credential access, commit, push or deployment occurred.
(Superseded by the next addendum for the attempt itself.)

## Addendum — November 9, 2026: first real sandbox attempt and removal of `_elements`

The owner ran the single bounded attempt for the approved official synthetic patient at 01:03 EST.
It succeeded: all eight operation profiles completed (9 requests, 4.14 s, one page per operation, no
continuation links), the 4101 handling above was confirmed against the real service, and publication
stayed disabled. Multi-page continuation was therefore **not** exercised. Epic also returned
informational code 59100, which Epic documents as "content invalid against the specification or a
profile" (for example an unsupported search parameter). The only difference between this adapter's
requests and the November 6 M1 client's was an `_elements` parameter on the DocumentReference search,
which is the **likely but unconfirmed** source of 59100 (the report aggregates codes by attempt, not by
operation). It signals an ignored parameter, not incomplete retrieval, and it cannot explain why this
patient's clinical-note search returned 4 documents against 53 on November 6, or vital signs 72 against
73; those differences are **unexplained** (shared-sandbox data drift is the more likely cause, unproven).
Do not describe the LIVE note or vital sets as complete.

Change (owner-approved): the DocumentReference search no longer sends `_elements`; every search now
sends only `patient` (and `category` where applicable), the same shape as the November 6 client.
The earlier wording that the request asked Epic for "metadata only" was inaccurate: Epic is not relied on
to project fields. Metadata-only extraction is enforced locally: `retain_resource` keeps metadata fields
and strips inline bodies, nested patient references are checked first, no attachment URL or Binary is
requested, and note bodies remain outside policy. The focused test now asserts that no search carries any
parameter other than `patient`/`category`. Re-run offline (guarded, five process-kill/recovery cases
deselected): **231 passed, 0 failed/skipped**; Ruff check and format pass. Whether 59100 disappears can
only be seen on a future, separately authorized LIVE attempt; none was made for this change.

## Addendum — November 9, 2026: staging-only LIVE refresh in the owner route (superseded below for staging)

`create_mock_beta_app` takes an optional `live: LiveRefresh` (signing key, sandbox configuration, an
explicit immutable `LiveAllowlist`, and for tests only a `MockTransport`). Without it nothing changes.
With it, the owner `refresh`, `status` and `revoke` routes also recognise an alias in that allowlist
(empty by default, so no alias resolves). A LIVE refresh is admitted under the LIVE policy, runs
`LiveAcquisition` in the same cancellable, revocation-checked work slot as a MOCK refresh, and records
content-free per-operation counts and informational outcome codes in the attempt row. There is no
generation and no publication, and nothing that can be served, selected, run against or cited. Every other
route (`select`, `rollback`, `runs`, reviews) and the browser patient list stay MOCK-only, so a LIVE alias
answers 404 there. `LiveAcquisition` attributes each informational code (for example 4101, 59100) to the
operation that returned it (`informational_codes` in the attempt's `operations`); `status` reports
`mode: LIVE`, `source: epic-fhir-sandbox-r4`, `serving: unavailable`, `publication: disabled` for such an
alias. A failed or disabled LIVE attempt finishes as `failed` with its code rather than staying queued.

## Addendum — November 9, 2026: immutable allowlist and private candidate staging with erasure

Owner-approved LIVE policy, first increment (local and offline; fake synthetic transports only):

- **Allowlist.** `beta_catalog.LIVE_PATIENTS` is removed. `LiveAllowlist` (in `beta_live_staging.py`) is a
  frozen, validated mapping: at most one patient, MOCK aliases/patient IDs and malformed or traversal IDs
  rejected at construction, detached from its source dict. It is passed explicitly through `LiveRefresh`
  and `LiveAcquisition`; the default is empty. (The consumed single-use driver is byte-preserved but can no
  longer construct the adapter; its attempt marker already blocks any rerun.)
- **Private staging.** A validated LIVE candidate (normalized bundle + manifest) is written to
  `live-staging/` beside the control catalog, never under a Git work tree (startup fails if it would be),
  and never read back, indexed, promoted or served. One candidate per alias; a newer one supersedes the
  older. POSIX: directory 0700, files 0600, owner and mode verified. Windows: modes are not applied; the root must be inside the user's profile or temp directory and not a link/junction, and (from the next addendum) its DACL is verified before any candidate is written. A catalog table holds content-free rows
  only (state, size, hash, times, erase reason).
- **Retention and erasure.** Maximum 24 hours (`STAGING_RETENTION_S`), enforced at startup, on every
  LIVE-capable request, at shutdown and by a background pass every 60 seconds. Earlier erasure on: patient revocation, owner revocation,
  supersession, owner request (`POST /beta/epic/patients/{alias}/candidate/erase`, which also cancels an
  in-flight LIVE attempt), and a failed or interrupted write. Erasure never follows a link, clears the
  read-only attribute if needed, and a failed erase leaves an `erase_pending` row that is retried and is
  visible in `status`. Startup removes interrupted writes and any file the catalog does not own.
  **Erasure is deletion, not a forensic wipe**: no overwrite, and the process may hold a candidate in memory
  until released. While the process runs, an expired candidate is erased within one purge interval (60 s); if it is not running, files persist past 24 hours until the next start.
- **In-flight revocation.** Revocation and owner erase cancel the LIVE task first and erase second; the
  write itself has no `await`, re-checks authorization at its last boundary and cleans up on any failure,
  so a revoked or cancelled refresh cannot stage a candidate late.
- **Not in this increment** (separate review): promotion/publication of a LIVE generation, a 30-day
  generation retention (policy recorded, no generation can exist yet), erasure of generations and their
  state, browser exposure, clinical runs on LIVE data (no enable route), real credentials, and fresh
  authentication for approval (to be required once a genuine authentication mechanism exists).
- **Verification (offline, guarded).** New `tests/test_beta_live_staging.py` (27 cases, 26 run on Windows) and the rewritten
  `tests/test_beta_live_route.py` (18 cases; the adapter file still has 75); the wider focused run (adapter, owner refresh, generations,
  browser bootstrap, session activity, owner workflow and acceptance files, five process-kill/recovery cases
  deselected) is **439 passed, 1 skipped (POSIX permission bits, not runnable on Windows), 0 failed**. Ruff
  check and format pass; mypy reports no errors in the touched files. No real Epic request, credential
  access, commit, push or deployment occurred.

## Addendum — November 9, 2026: evidence-only LIVE generations and the mandatory private-storage check

Owner-approved increment 2 (local and offline; synthetic fixtures and a fake transport only; no LIVE request):

- **Private-storage gate (applies to the staging added earlier too).** Before any LIVE-labelled byte is written,
  `verify_private_path` must pass for the storage root and again for the new directory. Windows: the real DACL is
  read (`GetNamedSecurityInfoW`) and access may be granted only to the current user, SYSTEM and Administrators
  (CREATOR OWNER / OWNER RIGHTS placeholders are allowed; they grant nothing beyond the checked owner); the owner
  must be the current user or Administrators; a missing DACL, any other principal, an unknown ACE type or an
  unreadable descriptor fails closed with `acl_unverified`. POSIX: owner and mode (no group/other bits). The check
  is unconditional for staging (an earlier version keyed it on the manifest's mode label, which a caller could influence; fixed after the security review) and for promotion.
  Python 3.13's `mkdir(mode=0o700)` creates exactly such a restricted ACL on Windows, so directories this code creates
  pass; a pre-existing directory with inherited broader access (this machine's Temp and profile directories grant
  other accounts access) is refused and must be hardened first. Nothing changes an ACL.
- **Manifest.** `LiveAcquisition` adds, before the manifest is hashed at staging: per-operation
  `informational_codes`, `completeness: "unconfirmed"` (a code constant; clearing it needs a new decision and evidence
  from an authorized attempt) and `synthetic_transport` (true only when an offline transport was injected).
- **Evidence-only store.** New `beta_live_generations.py` (`LiveGenerations`): own table `live_generation` and own
  `live-generations/` directory beside the catalog; `beta_generations.py` is not modified. A generation is
  `objects/<gid>/{manifest.json, normalized_bundle.json}`, re-verified on every load; there is no runtime state, no
  pin and no cached runtime. The tier comes from this store, never from the manifest, and must match exactly: mode LIVE,
  the Epic source, the LIVE policy and manifest version, the unconfirmed marker, the alias and the patient behind it
  in the immutable allowlist. MOCK, fake-transport and wrongly-labelled manifests are refused.
- **Approval.** `POST /beta/epic/patients/{alias}/candidate/{attempt}/approve` with the header
  `X-Beta-Candidate-Hash` equal to the `manifest_sha256` shown in the status candidate summary (bound to attempt and
  bytes), behind the existing Origin/CSRF boundary. It is a data-governance action, not a clinical review. The
  300-second recent-login check is a local/offline safeguard only, not reauthentication; **real LIVE data cannot be
  promoted** (`real_promotion` is False and nothing sets it) until genuine owner authentication and authorization
  exist. It also refuses while a LIVE refresh is in flight.
- **Atomic and failure-safe.** The candidate is fully validated first (tier, hashes, a throwaway ingestion check that
  is always erased, a private-storage check, a read-back of the written bytes, the attempt fence). Only then, with no
  `await`, one catalog transaction demotes the previous generation, inserts the new one and moves the patient pointer;
  a database constraint allows at most one approved generation per alias. Any failure before the commit removes the
  partial files and leaves the previous generation active and intact; the superseded generation is erased after the
  commit and retried if that fails; a crash after the commit is finished by the startup sweep.
- **Retention and erasure.** Maximum 30 days from approval (enforced at startup, on every LIVE-capable request, at shutdown and by the 60-second background pass), earlier on patient or owner revocation, supersession, `POST .../generation/erase` and the other erasure
  paths of the staging store. Erasure is deletion, not a forensic wipe.
- **Status.** The LIVE status shows a content-free `candidate` review summary (hash, provenance, counts, coverage,
  codes, completeness) and a `generation` summary. `serving` stays `unavailable` and `publication` stays `disabled`:
  nothing is served, no record route, no browser entry, no clinical access, no enable route.
- **Verification (offline, guarded).** New `tests/test_beta_live_generations.py` (54 cases, including real `icacls`
  widening and restoring of directory ACLs on Windows) plus the existing LIVE and MOCK files: **493 passed, 1 skipped
  (POSIX permission bits, not runnable on Windows), 5 deselected, 0 failed**. Ruff and mypy are clean on the touched
  files. No real Epic request, credential access, commit, push or deployment occurred. The 59100 attribution and the
  53 -> 4 / 73 -> 72 differences remain unconfirmed.

## Addendum — November 9, 2026: security-review hardening (S1, S2)

A focused read-only review of the evidence-only generation increment found two items to fix before any real data is
persisted; both are fixed (offline):

- **S1, unconditional storage check.** `LiveStaging.stage` previously ran the private-storage check only when the
  manifest said exactly `"mode": "LIVE"`, a label the caller controls (another value, a different case, a duplicate JSON
  key, or real traffic proxied through a mock transport would skip it). It now verifies the storage root and the new
  directory for every candidate. Generation promotion was already unconditional.
- **S2, time-driven retention.** Retention was enforced only at startup, on authenticated requests and at shutdown, so an
  idle running process kept expired data. The Beta app now runs an internal purge every 60 seconds
  (`LIVE_PURGE_INTERVAL_S`) while LIVE is configured: no network, no refresh, nothing exposed. A pass that fails is
  audited (`live_purge_failed`) and the next pass retries; the task is cancelled at shutdown before the catalog closes.
- **Not changed:** S4 (a real-process-kill test for LIVE approve) remains open as a hardening item. S3 (verify each
  directory immediately after `mkdir`, before writing) was fixed afterwards; see the S3 section at the end of this file.
- **Verification (offline, guarded).** New tests: every label variant refused on unverified storage (8 cases, including an
  end-to-end refresh), idle purge of an expired candidate and generation with no request made, and a failed pass that is
  audited without stopping the loop. Reproducing the old behavior in-process turns exactly those tests red (8 and 2).
  Full Beta set including the five MOCK process-kill cases, nothing deselected: **507 passed, 1 skipped (POSIX
  permission bits, not runnable on Windows), 0 failed**. Ruff and mypy clean on the touched files.

## S3 hardening (offline)

S3 FIXED (offline, local, uncommitted, 2026-11-09): new `make_private_directory` (mkdir, then `verify_private_path`) in `beta_live_staging.py`, used for the staged `.tmp` directory (`LiveStaging.stage`), `approve`'s `.obj` temporary directory and the throwaway `.check` directory (`_ingest_check`, whose mkdir moved inside the try so a failed verification is still erased). Evidence: 3 new tests in `tests/test_beta_live_generations.py` (verifier fails on the new directory only: zero writes, nothing left, candidate still approvable); the pre-S3 order turns all 3 red; focused LIVE set (generations, staging, route) 110 passed, 1 POSIX-only skip; Ruff and touched-file mypy clean. Offline validation only, not production readiness; MOCK paths untouched.

## AUTH-1 (offline)

AUTH-1 IMPLEMENTED (offline, local, uncommitted, 2026-11-09; owner-approved after S3; defaults chosen: 120 s one-shot elevation, only LIVE approve is sensitive, refresh needs none). `api/beta_oidc.py`: immutable `OwnerIdentity` (https issuer, client id, exactly one owner subject, required `amr`/`acr`, `mfa_at_login`, `max_auth_age_s`, `step_up_ttl_s`; default `INVENTED_IDENTITY` keeps the invented offline identity), id_token `auth_time`/`acr`/`amr` validated and stored on the session (`AuthClaims`), a separate session-bound step-up flow (`OwnerAuth.begin_step_up` / `complete_step_up`: `prompt=login&max_age=0`, single-use flow bound to the asking session token and CSRF, `auth_time` must be present, not future and no earlier than the flow start minus 5 s, MFA claims required, same live session after the token await) and a one-shot `Elevation` (`consume_elevation`, cleared on first use whatever the outcome, 120 s, bound to session and CSRF). `api/beta.py`: `POST /beta/auth/step-up` exists only when LIVE is configured; the callback dispatches by flow purpose; LIVE approve's `fresh` callback now consumes the elevation (`FRESH_LOGIN_S` and the session-age check removed; error code `step_up_required`); `create_mock_beta_app(identity=)`. `beta_live_generations.approve` validates the candidate-hash format BEFORE consuming the elevation. `real_promotion` stays False and nothing sets it (guard test). MOCK rollback keeps its own session-age rule (unchanged). Verification: new `tests/test_beta_step_up.py` (48 tests: identity validation, 16 rejected and 3 accepted claim cases, ACR, single owner subject, replay, expiry at 119/121 s, one-shot, forged CSRF/Origin do not spend it, new session and logout discard it, flow cannot elevate another session, login stores claims, MFA-at-login, route absent without LIVE, `real_promotion` guard); in-process mutations (no one-shot clear, no fresh auth_time, no MFA, no expiry, no session/CSRF binding) each turn tests red; full Beta set, nothing deselected: 558 passed, 1 skipped (POSIX-mode test), 0 failed (507 + 3 S3 + 48 AUTH-1); Ruff and touched-file mypy clean. NOT genuine authentication: the identity provider is still the invented in-process one, there is no JWKS/discovery/rotation and no real MFA (AUTH-2). Offline validation only, not production readiness. No Epic request, commit, push, deployment or install; driver, marker, credentials and Stable untouched.

## S4 hardening (offline)

S4 DONE (offline, local, uncommitted, 2026-11-09; tests only, no product code changed): `test_a_killed_process_leaves_the_previous_generation_intact_and_restart_recovers` in `tests/test_beta_live_generations.py`, five cases (`after_check`, `after_tmp`, `after_rename`, `before_commit`, `after_commit`). A child Python process (offline socket guard installed) opens the same catalog and LIVE stores, runs `LiveGenerations.approve` with a hook that writes a marker and blocks, and the parent kills it with `Popen.kill()` (Windows TerminateProcess; no cleanup runs). The test first asserts what the kill left on disk (tmp `.obj`, unowned object directory, or committed row, so each case really stopped at its boundary), then restarts from fresh `Catalog`/`LiveStaging`/`LiveGenerations` instances and asserts: exactly one approved row; the pointer (`patient_head.active`) and `view()` name the previous generation (or the new one after the commit); the previous generation's file hashes are unchanged and it re-verifies on load; no orphan object or tmp directory; before the commit the candidate is still staged and a retried approve succeeds with the same generation id; after the commit the superseded generation and the promoted candidate are erased by the startup sweep. Mutation checks (sweep no longer removes orphan objects, orphan tmp, or the promoted candidate) turn 2, 1 and 1 cases red. Full Beta set, nothing deselected: 563 passed, 1 skipped (POSIX-mode test), 0 failed (558 + 5). Limits: a process kill is not a power-loss or host-crash test (Windows has no directory fsync; no durability claim), and SQLite commit atomicity is relied on, not independently proven. Ruff clean. No Epic request, commit, push, deployment or install; driver, marker, credentials and Stable untouched.

## Addendum — November 13, 2026: owner-attended acquisition, promotion and local serving

**Acquisition.** The owner separately approved one bounded acquisition for the approved official synthetic patient. It ran through the genuine-owner launcher: an attended key unlock, a fresh Windows Hello proof, and a new private store and acquisition ID.
- All eight operation profiles completed: 9 requests, all HTTP 200, one page each, so multi-page continuation was again not exercised.
- Informational code 4101 appeared only on the empty encounter-diagnosis search.
- **No 59100** was returned. This is consistent with the November 9 removal of `_elements`, though one run does not prove the cause.
- Counts were 80 resources (Patient 1, Encounter 1, Condition 2, DiagnosticReport 2, Observation 74) and 33 note metadata entries. Vital signs returned 73 (72 on November 9). DocumentReference returned 33, against 53 on November 6 and 4 on November 9; this remains unexplained sandbox variation.

**Candidate review.** A read-only review found every resource bound to the approved patient with Epic lineage and content hashes, and found:
- one retrieval instant;
- the retrieval time substituted only for missing record metadata;
- no narrative;
- no note bodies or attachments;
- one unresolved DiagnosticReport result reference.

**Promotion and serving.** After a separate approval, the candidate was promoted. The generation's manifest and bundle hashes matched the reviewed candidate, and the staged copy was erased only after commit.

A further separate approval allowed read-only serving to the local Beta. The eight-page UI showed the verified record, provenance and coverage after fresh owner proofs. Out-of-phase and forged requests were refused.

Completeness stays `unconfirmed`. Publication, MedicationRequest, Binary and note bodies stay disabled. Identifiers, hashes and receipts are kept in the owner's private local records, not in Git.
