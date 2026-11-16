# Hosted LIVE Epic Beta: architecture, security and runbook

Implements the minimal path in `docs/EPIC_LIVE_HOSTED_PLAN.md`: hosted browser → authenticated hosted backend → real
Epic OAuth → fresh Epic FHIR R4 sandbox requests → patient-scoped validation, mapping and provenance → the Beta UI. It
runs entirely on Cloudflare, independent of the owner's workstation, a local server or Windows Hello. It is
**owner-only and private**: it is not linked from Stable, it is not the public demo, and Epic's permission for public
display remains a separate, open question.

## Status (2026-11-16)

- **Built, deployed, guarded.** Built and tested through `d39c575`, with CI green on all seven jobs. Deployed as an
  owner-only Worker behind Cloudflare Access, which allows exactly one owner identity. The kill switch is off between
  tests.
- **Authentication works end to end.** Cloudflare Access, the Worker gate and the backend container each verify the
  owner's token.
- **First genuine hosted test.** The backend made one Epic request, the OAuth token request, and Epic answered
  HTTP 400. No FHIR request was made and no patient data was staged. The likely cause is that Epic had not yet
  synchronised the newly published hosted key: the call came about 36 minutes after publication, below this
  project's 70-minute minimum. One owner-approved retry follows.
- **Two Workers-runtime defects fixed.** Both were in the gate's Access key fetch, and Node tests could not catch them:
  - `redirect: "error"` is not supported by Workers' `fetch`;
  - calling the global `fetch` with a foreign receiver throws "Illegal invocation".

  Both now have tests that mimic Workers' `fetch` behaviour.
- **Acceptance goal met, 2026-11-16.** One owner-approved retry ran about 72 minutes after the key was published.
  - Epic accepted the hosted key: OAuth succeeded, then all eight read-only FHIR operations returned HTTP 200.
  - The owner reviewed the validated candidate, approved it by its exact hashes, and the approved Epic sandbox
    synthetic patient's record rendered in the hosted Beta with provenance, coverage and freshness.
  - Everything ran on Cloudflare, independent of the owner's workstation.
  - Afterwards the hosted data was erased and Epic retrieval switched off again.

## Components

| Piece | Where | What it reuses |
|---|---|---|
| Worker `clinical-trace-ai-beta-live` | `deploy/cloudflare/beta-live/` (`wrangler.jsonc`, `src/index.js`, `src/gate.js`) | Static Beta assets with the existing `_headers`/CSP from `release-beta.mjs` |
| Backend container | `deploy/cloudflare/beta-live/backend/Dockerfile`, entry `python -m api.beta_hosted.hosted` | `LiveAcquisition`, `Catalog`, `LiveStaging`, `LiveGenerations` (real promotion), `check_tier`, `live_record_view(genuine_only=True)` |
| Hosted backend code | `api/beta_hosted/` (`hosted.py`, `access.py`, `hosted_keys.py`) | Candidate review identical to the owner coordinator; `parse_jwks`, `bounded_response` |
| Live UI | `frontend/beta/live.ts`, `frontend/beta/LiveBetaApp.tsx` (`VITE_BETA_CLIENT=live`) | `BetaShell`, `RecordTabs`, `RefreshStatus`, `Provenance`, `WarningList`, `DataOrigin`, `CandidateSummary` |
| Release | `deploy/cloudflare/scripts/release-beta-live.mjs` | `requirePublishedTip`, `assemble`, `forbiddenReferences`, `verifyAccount` |
| Secret provisioning (owner-run) | `scripts/provision_beta_live_secrets.py` | `keys.generate_private_key`, `public_jwk`, `build_jwks`, `load_config` |

The invented-data demo Worker `clinical-trace-ai-beta` and Stable are unchanged. The demo stays the rollback.

## Request flow

1. **Cloudflare Access** guards the whole `clinical-trace-ai-beta-live` workers.dev hostname. Only the owner's email,
   after the identity provider's login (one-time PIN or the chosen provider with MFA), receives an application token.
2. **Worker gate** (`gate.js`), for `/beta/api/*` only:
   - verifies the Access token again: RS256, the team's own keys (one shared fetch per isolate), issuer, AUD tag,
     times, an application (not service) token, and the configured owner email (ASCII only, compared
     case-insensitively in ASCII, the same way in both layers);
   - checks the route and refuses query strings;
   - requires `X-Beta-Client: live` and same-origin fetch metadata on every API route (so another site cannot even
     wake the container), plus the exact `Origin` on POST;
   - applies a rate limit, and refuses Refresh while the kill switch is off;
   - forwards only four headers to the single Durable Object `owner`.
3. **Durable Object** starts the one container instance (`max_instances: 1`, `lite`: 1/16 vCPU, 256 MiB, 2 GB disk).
   Configuration and secrets are passed only at start. A configuration change restarts the container. It sleeps after
   10 idle minutes (each request resets the timer).
4. **Backend** (`api/beta_hosted/hosted.py`) verifies the Access token a third time. Then:
   - **`POST /beta/api/refresh`:** Catalog admission (5-minute cooldown, 2 per patient per hour, 6 per hour in total,
     240 requests per hour), then `LiveAcquisition`. That is RS384 `private_key_jwt` OAuth, followed by the eight
     read-only operations for the one allowlisted patient, pinned to `fhir.epic.com`, with the existing byte, time
     and request budgets. Next come `check_tier`, the genuine-transport gate and the coordinator's candidate review.
     The candidate is staged and returned for review, without being displayed.
   - **`POST /beta/api/approve`** with the exact attempt ID and manifest SHA-256: `LiveGenerations.approve`, including
     its runtime ingest check. The endpoint returns the verified view.
   - **`GET /beta/api/record`:** `live_record_view(genuine_only=True)`, served with its freshness label, the
     `hosted_owner_only` notice and clinical AI disabled.
   - **`GET /beta/api/status`**, **`GET /beta/api/session`**, **`POST /beta/api/erase`:** content-free status, the
     session, and erasure.

Invented fixtures can never be displayed. A fake transport yields `synthetic_transport: true`, which the staging
gate, the projection and the live client each refuse. The live build contains no fixture data (the release checks
this).

## Security properties and limits

- **Credentials.** A separate hosted-only RSA key with its own `kid` is added to the published JWK Set. The local
  workstation key is never copied. The private key exists only in the Worker secret store and the container's
  environment.
  - The backend parses the key at start and removes it from `os.environ`. The container's initial process
    environment (`/proc/1/environ`) still holds it, readable only inside the container by its own user.
  - Revocation: remove the `kid` from the JWK Set and delete the Worker secrets.
- **Network boundary.** The container has no public address; only this Worker's Durable Object binding reaches it.
  Outbound internet is enabled for Epic and for the Access key endpoint. Epic hosts stay fixed by the existing client
  and `settings`.
- **Scope.** One patient (the allowlist) and the eight read operations. MedicationRequest, Binary and note bodies are
  off; there is no free-text patient search.
- **Storage.** Ephemeral container disk (`/data`, mode 0700, non-root user, code read-only). A container restart
  erases staged and approved data. Retention rules (24 h staging, 7-day serving window) still apply while it runs.
- **Logs.** Worker observability is off and container access logs are off. Catalog audit codes are content-free.
- **Not done (deliberate, minimal scope).** There is no step-up beyond the Access session, so keep that session
  short. There is no multi-user model, no persistent storage, and no public display.

## Independent security review (2026-11-15)

No critical or high findings. All six findings were fixed and tested:

- shared Access key fetch on a cold isolate (medium);
- client header on GET routes;
- the docstring now states the `/proc/1/environ` limitation;
- ASCII-only email comparison;
- symlink refusal in the build-context stager;
- POST bodies accepted chunked within the 4 KiB limit.

## Cost (checked 2026-11-15)

- **Plan.** Cloudflare Containers needs the Workers Paid plan: USD 5/month.
- **Included usage.** The plan includes 25 GiB-hours of memory, 375 vCPU-minutes and 200 GB-hours of disk per
  month. Memory and disk are billed only while the instance is awake.
- **Expected use.** A `lite` instance (0.25 GiB) used for a few short sessions a month stays far inside the included usage: 25 GiB-hours is about 100 awake hours.
- **Access.** Cloudflare Access (Zero Trust Free) covers up to 50 users.

## Cost controls

- **One small instance.** `max_instances: 1`, `lite` instance type, and a 10-minute idle sleep.
- **No anonymous starts.** Anonymous or misconfigured requests never start the container: the Access check and the
  configuration check both run before the Durable Object is called.
- **Bounded traffic.** The Worker rate limit is 30/min. The Catalog's Epic budgets and the kill switch stay mandatory.
- **Minimal paid testing.** No load or stress tests and no monitoring loops run against paid infrastructure. The real
  hosted tests are limited to auth refusals, one Fetch → Review → Approve, and the final browser check.
- **Billing controls** (checked 2026-11-15):
  - Budget alerts (Billing → Billable Usage) only notify.
  - Spend policies with `usage_block` exist for the `workers` and `durable_objects` products. They block further
    usage until the period ends.
  - Containers are not a listed spend-policy product. Because a container can only be started through its Durable
    Object, a `durable_objects` usage block should also stop new container starts. That is inferred, not documented.

## Runbook (each step needs the owner approval named in AGENTS.md)

1. **First deploy.** From the pushed tip of `feature/beta`:
   `node scripts/release-beta-live.mjs --first-deploy` (in `deploy/cloudflare`). This needs Docker running. The kill
   switch is off and there are no secrets yet, so the API answers `503 backend_not_configured` and no container
   starts.
2. **Access.** Workers & Pages → `clinical-trace-ai-beta-live` → Settings → Domains & Routes → workers.dev →
   **Enable Cloudflare Access**. Then **Manage Cloudflare Access** and allow only the owner's email. Note the team
   domain and the application's AUD tag.
3. **Secrets.** Run `python scripts/provision_beta_live_secrets.py` on the owner's workstation. It sets eight Worker
   secrets and writes the merged public JWK Set outside the repository.
4. **JWK Set.** Publish the merged file to the `epic-jwks` branch (GitHub Pages). Verify it with
   `keys.check_published_jwks` for the new `kid`.
5. **Guarded deploy.** Run `node scripts/release-beta-live.mjs`. The smoke requires the Access redirect.
6. **Live test.** Run `node scripts/release-beta-live.mjs --enable-epic`. The owner signs in, presses **Fetch from
   Epic**, reviews the candidate, approves it, and the record renders. Record the Epic HTTP outcomes and hashes.
7. **If Epic refuses the token (HTTP 400):**
   - Do not retry, and leave the kill switch off.
   - In the Epic portal, re-save the app with **Save & Ready for Sandbox** and no field changes.
   - Wait 70 minutes to 24 hours, then run one newly approved retry.
   - Error bodies are never captured. Any diagnostic must stay content-free, and it touches hash-pinned code, so it
     needs review first.
8. **Afterwards.** Redeploy without `--enable-epic` (kill switch off) and erase the hosted data. Optionally revoke
   the hosted `kid`.
