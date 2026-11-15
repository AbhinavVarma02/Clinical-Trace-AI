# Hosted live Epic FHIR R4 integration: final objective and next phase

Recorded 2026-11-15 (America/New_York). This is a planning document; nothing here is implemented or authorized yet.
Every implementation step, hosting choice, credential action, live Epic call and deployment needs its own owner
approval.

## Final objective (owner-defined)

**A fully hosted, live Epic FHIR R4 integration:**

```
Hosted Beta browser
  → authenticated Beta backend
  → real Epic OAuth authentication (Backend Services, private_key_jwt)
  → fresh Epic FHIR R4 sandbox API requests
  → patient-scoped FHIR validation
  → clinical mapping and provenance
  → hosted Beta UI
```

It must work without the owner's laptop, a local server or Windows Hello.

**These do not complete the project:**
- **The invented-data demo now live on the Beta Worker.** It is temporary infrastructure validation. It shows invented
  application fixtures and contacts no Epic system.
- **A static snapshot of Epic-acquired data.** Even an owner-approved publication copy of a real Epic sandbox
  retrieval is frozen, never refreshed, and needs no live OAuth or API call.

## Two kinds of "synthetic" data

| Term | What it is | Where it appears |
|---|---|---|
| **Invented application fixtures** | Records written by hand for tests and demos, for example "MOCK Alpha (synthetic)" in `frontend/beta/fixtures.ts`, the MOCK transport and test fixtures. No Epic system produced them. | Unit tests, the local MOCK Beta, and the hosted invented-data demo |
| **Epic sandbox synthetic patients** | Fictional test patients that Epic publishes in its open.epic FHIR R4 sandbox, for example Warren McGinnis, returned by Epic's real API after real OAuth. They are provider-acquired evidence, not invented by this project. | The local genuine Beta (accepted 2026-11-13), and the final hosted objective |

The UI already labels both: the data-origin badge, the warnings and the provenance panel. An invented fixture must never
be labelled as Epic data, and Epic data must never be routed through a fixture path. Both the publication path and the
release script enforce this.

## Where things stand (2026-11-15)

| Area | State |
|---|---|
| Stable 2.0 | Unchanged in production. The edge Worker was last deployed 2026-10-18 and the Hugging Face Space backend 2026-10-18. `main` has the OpenTelemetry 1.45.2 fix (PR #4, merged; no deployment). |
| Local genuine Epic Beta | Accepted 2026-11-13 for one patient: a real OAuth and FHIR acquisition, review, promotion, and read-only serving behind Windows Hello on a loopback origin. |
| Hosted Beta Worker | `clinical-trace-ai-beta` (assets only). It serves the **invented-data demo** at <https://clinical-trace-ai-beta.vathadiabhinav.workers.dev/beta/app/>, first deployed 2026-11-15 from `636079b` (version `5809e6dc-850a-4764-aaac-bbeca6ff201b`). |
| Publication-copy path | Built and tested, but not used. Epic's permission for public display is pending. |
| Stable Beta link | Draft PR #5 (unmerged), gated by `--with-beta-link`. |

## Reusable without rewriting

**Epic OAuth and FHIR (`src/interop/epic/`):**
- `auth.py`: Backend Services `client_credentials` with an RS384 `private_key_jwt` assertion.
- `keys.py`: JWK Set and `kid` handling.
- `client.py`: a read-only client, bound to one patient and pinned to the Epic sandbox host.
- `beta_acquisition.py`: bounded acquisition with `Limits` (deadline, per-request, response bytes, request budgets).
- `beta_live_acquisition.LiveRefresh`: the default-disabled live adapter; the unlocked key is injected.
- `mapping.py`: field status, lineage and content hashes.
- `beta_staging.py`: the FHIR gate.
- `beta_live_staging.py`: the immutable allowlist and validated staging.
- `beta_live_generations.py`: hash-bound promotion, retention and erasure.
- `beta_catalog.py`: content-free attempt and operation audit, cooldowns, patient and global budgets.
- `telemetry.py`: content-free counters.

**Projection (`api/`):**
- `beta_live_record.live_record_view(genuine_only=True)`: verified LIVE view with coverage, provenance and warnings.
- `beta_record.py`: the field-by-field browser-safe projection.
- `beta_publication.py`: publication copies only.

**Browser API (`api/beta.py`).** It already exposes OIDC login and MFA step-up, sessions, patients, record, status,
refresh, candidate approval, generation select and rollback, runs and reviews. It takes an optional `live=LiveRefresh(...)`
(staging only today). `beta_oidc.py` and `beta_oidc_keys.py` provide pinned provider metadata, rotating JWKS and
`amr`/`acr`/`auth_time` checks; they are tested offline only (AUTH-2a).

**Frontend (`frontend/beta/`).** The eight-page shell, the HTTP client for the browser API, the record, coverage,
provenance and warning components, and the freshness and serving badges (`RefreshStatus`).

**Hosting (`deploy/cloudflare/beta/`).** The assets-only Worker, `scripts/release-beta.mjs` with its guards, smoke test
and rollback, and CI coverage. The Stable edge (`deploy/cloudflare/src`) already shows a guarded Worker → backend proxy
with a shared secret, rate limits and a route allowlist.

## What prevents the hosted Beta from calling Epic today

1. **No hosted backend.** The Beta Worker serves static files only. The Python Beta backend has run only on the owner's
   machine.
2. **Credential custody.** The Epic signing key is a passphrase-encrypted local file. It is unlocked only in the owner's
   attended console, with Windows custody checks. No hosted secret store holds an Epic key.
3. **Authentication.** Genuine owner authentication is Windows Hello WebAuthn on a loopback origin, with NTFS custody
   and an attended console, and is Windows-only by design. There is no hosted identity provider: AUTH-2b and AUTH-2c
   are paused, and the OIDC seam is offline-tested only.
4. **Storage.** LIVE staging, generations and the catalog need private local directories (ACL or mode checked) and
   SQLite. No hosted persistent, private storage has been chosen.
5. **Code gates.** All of these are default-disabled and owner-bound today:
   - LIVE refresh in the browser API is staging-only;
   - real promotion and LIVE serving need an owner binding and pinned evidence;
   - the acquisition adapter needs an explicit process-local enable.
6. **Permission and terms.** Epic has not confirmed that its sandbox data may be displayed publicly, whether live or as
   a copy. A hosted service using the registered non-production Backend Systems client is also unconfirmed.

## Minimum new work

**Backend.** Add a new `api/beta/` package for new hosted functionality. Existing tested modules are not moved, because
their paths are hash-pinned by the private runners and their evidence.
- `api/beta/hosted.py`: a factory composing the existing pieces, namely the browser API, real OIDC provider metadata,
  `LiveRefresh`, staging, generations, `genuine_only` projection and the allowlist. It is configured from environment
  and secrets, fails closed, and keeps clinical AI disabled.
- **Secret key loading:** a bytes or PEM-from-secret variant beside `keys.load_private_key(path, …)`.
- **Storage adapter:** a persistent private volume with the existing directory checks, or an explicitly ephemeral
  mode. This is an owner decision.
- **Health and readiness endpoints** with no clinical content.
- **A separate `Dockerfile.beta`.** The Stable image and its `.dockerignore` allowlist stay unchanged.

**Hosting.**
- A container host for the Beta backend (decision D2). Free Hugging Face Spaces can no longer be created, and the cost
  constraint applies.
- A front door, either of:
  - the Beta Worker proxying `/beta/api/*` to the backend with a shared secret (reusing the Stable edge pattern);
  - the backend serving the UI directly.
- Secrets live only in the host's secret manager, never in Git, Worker assets or CI.

**Frontend.** A `live` client mode reusing the HTTP client and the existing record and freshness components. It needs
no redesign; the eight pages stay as they are.

## Security design to settle before building

- **OAuth credentials.**
  - A separate, hosted-only, non-production key pair with its own `kid`, added to the existing public JWK Set. The local
    key never leaves the laptop.
  - The private key lives only in the host secret manager and is loaded into memory at start.
  - Rotate by adding a new `kid`; revoke by removing it from the JWK Set.
  - Keep the existing read-only scopes and the eight initial profiles. MedicationRequest, Binary and note bodies stay off.
- **Hosted authentication.**
  - A real OIDC provider with MFA, through the existing `beta_oidc` checks.
  - An explicit subject allowlist covering the owner and, if approved, named reviewers.
  - Every request that reaches Epic needs an authenticated session, and step-up for refresh and promotion.
  - No anonymous route reaches Epic.
- **Patient allowlist.** Only Epic's documented sandbox test patients, as immutable configuration. No free-text patient
  search.
- **Patient isolation.** Reuse the patient-bound client, nested-reference checks, per-alias generations and the
  isolation tests.
- **Rate limits.** The existing `Limits` and catalog admission (cooldown, per-patient, global per hour, deadlines), plus
  per-subject limits and Worker rate limiting.
- **Auditability.** The content-free catalog (attempts, operations, HTTP outcomes, informational codes) and telemetry,
  with a log retention policy and no clinical content in logs.
- **Caching and freshness.** Generations, plus the existing serving labels (fresh within 24 h, cached, stale, at most
  7 days), retrieval timestamps on every view, and an explicit "unchanged since last retrieval" state. Retrieval time is
  never used as clinical time, and clinical dates never imply freshness.
- **Promotion policy for fresh requests.** Decide whether a validated fresh candidate is shown directly as a transient,
  labelled preview, or promoted after owner review. Today's design requires review.

## Tests and acceptance

- **Offline, required before any live call.**
  - Hosted-factory tests against the existing fake Epic (`MockTransport`, the test-only `FixtureLive`).
  - Authentication negative tests: no session, wrong subject, missing MFA, expired step-up.
  - Allowlist, isolation and budget tests, and secret-loading tests with invented keys.
  - Proxy and Worker tests.
  - Container build and smoke tests.
  - CI on Linux and Windows.
- **Genuine hosted end-to-end acceptance** (owner-approved and bounded), from an independent device on another network:
  1. Sign in.
  2. Trigger one fresh retrieval for one allowlisted patient.
  3. Check Epic HTTP outcomes, patient-scoped validation, mapping, provenance and hashes, and freshness labels.
  4. Check isolation, rate limits and the audit record.
  5. Then roll back or revoke the hosted key.

## Unresolved security and Epic questions

- **Epic's permission** to display sandbox test-patient data publicly, live or as a copy, and any attribution or access
  conditions.
- **Whether Epic permits** a hosted service to use the registered non-production Backend Systems client, and whether
  adding a hosted `kid` needs registration changes.
- **Hosting:** host selection under the cost constraint (D2), and account placement (D3).
- **Identity:** the identity provider and MFA choice, and the reviewer access model (owner only or named reviewers).
- **Storage:** hosted storage persistence, retention and erasure, which must follow the existing 30-day generation rule.

## Proposed milestone sequence (each separately approved)

| Step | Scope |
|---|---|
| L1 | Owner decisions: host, identity provider and MFA, reviewer model, key strategy, storage, promotion policy, Epic terms |
| L2 | Offline `api/beta/hosted.py` factory, secret key loading and storage adapter, with tests (no live calls) |
| L3 | Hosting and secrets provisioned, with no Epic key and the backend answering health checks only |
| L4 | Hosted authentication live with the subject allowlist; negative tests from an independent device |
| L5 | Frontend `live` client mode against the hosted backend, still with the fake Epic transport |
| L6 | Hosted Epic key registered; one owner-approved, bounded live retrieval through the hosted stack |
| L7 | Independent-device acceptance of the full flow; rollback and revocation drill |
| L8 | Stable Beta link via PR #5 and a `--with-beta-link` release |
