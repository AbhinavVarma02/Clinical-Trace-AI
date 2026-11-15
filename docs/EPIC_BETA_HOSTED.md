# Hosted Epic Beta: read-only published copy

**Status (2026-11-15): the Beta Worker is deployed with the invented-data demo, not with Epic data.**
- **Live:** <https://clinical-trace-ai-beta.vathadiabhinav.workers.dev/beta/app/>, Worker `clinical-trace-ai-beta`,
  version `5809e6dc-850a-4764-aaac-bbeca6ff201b`.
- **How:** first deployed by the owner-approved `node scripts/release-beta.mjs --invented-demo --first-deploy` from
  `636079b`, after CI run 43746264935 passed all six jobs.
- **Contents:** only the invented MOCK fixtures, clearly labelled. No Epic-acquired record is published.
- **Publication copy:** still held until Epic confirms that its developer-sandbox test-patient data may be shown on a
  public demonstration site.
- **Not the final objective.** Neither the demo nor a published copy is the final objective, which is a hosted, live
  Epic integration; see [EPIC_LIVE_HOSTED_PLAN.md](EPIC_LIVE_HOSTED_PLAN.md).
- Every further deployment step needs its own explicit owner approval.

*Earlier status (2026-11-14): implemented and tested offline only; nothing was published.*

## Live verification of the invented-data demo (2026-11-15)

- **Release:** exit 0, 6 files uploaded, the built-in demo smoke test passed. The receipt records `invented-demo`, no
  copy, first deploy, passed.
- **Routing:** `/`, `/beta` and `/beta/app` redirect to `/beta/app/`.
- **Headers:** strict `'self'` CSP, `X-Robots-Tag: noindex, nofollow, noarchive`, `Referrer-Policy: no-referrer`,
  framing denied, `nosniff`, HSTS, and a Permissions-Policy that denies WebAuthn. No cookies are set.
- **404s:** the published-record path, the build manifest, owner endpoints, the Beta API and the Stable API.
- **Integrity:** every served file is byte-identical to the local build.
- **Content scan:** no Epic-acquired identifiers, acquisition IDs or hashes, private paths, publication loader, owner or
  WebAuthn code, and no secret patterns. The invented fixtures and synthetic labels are present.
- **Browser:** all eight pages render with the synthetic banner on desktop and at phone width (375 px), with no
  horizontal overflow. Only 5 same-origin asset requests are made; no other origins, no storage and no console errors.
- **Independent network:** a remote fetch loaded the page, and the record path returned 404 there too.
- **Stable unchanged:** no new Stable edge deployment since 2026-10-18, and the Hugging Face Space is unchanged.

## Design

The hosted Beta is the smallest secure way to let another person explore the one approved Epic sandbox synthetic
patient without the owner's workstation, Windows Hello credential or localhost:

- **Static and read-only.** There is no backend, API, Epic credential, token, live Epic call, acquisition or
  promotion. The site serves one owner-approved *publication copy* of the already-promoted generation.
- **Same projection as the local Beta.** `scripts/run_owner_epic_export.py` reads the promoted generation through
  the same gates as the accepted serve phase:
  - custody, a read-only catalog and generation integrity;
  - the owner binding (it must not be revoked) and the pinned attempt, generation, manifest and bundle;
  - `live_record_view(genuine_only=True)`.
  
  `api/beta_publication.py` then withholds the acquisition attempt identity and labels the copy as a never-refreshed
  published snapshot. It adds no other data.
- **Same pages.** `VITE_BETA_CLIENT=published` builds the existing eight-page Beta shell with the existing record,
  coverage, provenance and warning components. The copy is fetched same-origin without credentials and is shown only
  if every check passes. Agent, Risk, Evaluation and Governance stay disabled. Operations has no owner functions. The
  build contains no owner or WebAuthn code.
- **Isolated hosting.** A separate assets-only Cloudflare Worker, `clinical-trace-ai-beta`
  (`deploy/cloudflare/beta/wrangler.jsonc`), on the dedicated account.
  - It has no script, Workers AI, D1, rate limits, secrets, cron or preview URLs.
  - It cannot use or affect the Stable edge's bindings or budgets.
  - Headers: a strict `'self'` Content-Security-Policy, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer`,
    framing denied, and `Cache-Control: no-store` on the copy.

## What the copy shows and withholds

**Shown:**
- The verified patient identity and every mapped section: conditions, encounters, labs, vital signs, reports and
  note metadata.
- Per-resource source references, versions and content hashes.
- Per-operation coverage and outcomes.
- Generation identity, retrieval and promotion times, policy, mapping version, and manifest and bundle SHA-256.

**Every known limitation stays visible:**
- Completeness is unconfirmed.
- Note bodies and medications are not imported.
- Unresolved references, substituted fields and informational codes are flagged.

**Withheld:** the acquisition attempt identity. No note bodies, attachments, upstream identifiers, credentials, owner
state or private evidence are part of the copy or the site.

## Limitations

- The copy is never refreshed. It shows the single approved retrieval, labelled with its retrieval time.
- `LiveGenerations` serves a generation for at most 7 days after retrieval. An export after that window needs a new,
  separately authorized acquisition.
- Epic's terms are an owner decision. The open.epic developer terms allow keeping Materials "for yourself" and
  distributing them "only by linking", and they do not address test-patient records or demonstrations.

## Runbook (each step needs its own owner approval)

1. **Export.** The owner runs it in their own attended console, as with the serve phase. It locks the process
   environment before any application module loads.
   - Command:
     `python scripts/run_owner_epic_export.py --accept-owner-policy --binding-root … --binding-sha256 … --private-root … --alias … --patient-id … --attempt-id … --generation-id … --manifest-sha256 … --bundle-sha256 … --out …/record.json`.
   - The output folder must already exist and be private.
   - After resolving junctions and links, it must be outside the repository, outside any Git work tree and outside the
     owner and Epic roots, and the owner and Epic roots must not overlap.
   - The file is created exclusively, never overwritten, and the command prints its SHA-256.
2. **Review.** Check the copy's content, labels and withheld fields. Record the approved SHA-256.
3. **Publish.** From `deploy/cloudflare`, run `node scripts/release-beta.mjs --first-deploy --copy … --copy-sha256 …`.
   Before any remote step, the release refuses:
   - a commit other than the pushed `feature/beta` tip;
   - a copy that does not match the approved SHA-256;
   - a copy that is not a fully consistent genuine Epic copy;
   - any build file other than the approved copy that contains loopback addresses or owner code.
   
   It always builds with invented fixtures refused. Next it deploys, then smoke-tests the app, its headers and the
   served copy's hash, with retries. A failed later release rolls back automatically; a failed first deployment is
   left for an operator.
4. **Link from Stable.** The Stable Beta entry renders only with `VITE_ENABLE_BETA_LINK=true`.
   - Draft PR #5 into `main` points it at the hosted URL and adds the release option `--with-beta-link`.
   - After PR #5 merges, a Stable release with that option, which keeps automatic rollback, ships the link.
   - Default Stable releases refuse the link.
5. **Remove.** From `deploy/cloudflare`, run `npx wrangler delete --config beta/wrangler.jsonc`, then revert the
   Stable link with a normal Stable release.

## Invented-data fallback (while Epic's permission is pending)

If Epic's confirmation is delayed, the same Worker can publish the existing **invented MOCK fixture** build of the
Beta, with no Epic data at all:

```
node scripts/release-beta.mjs --invented-demo --first-deploy
```

- **What it shows.** The eight-page Beta with two invented patients, "MOCK Alpha/Bravo (synthetic)", from
  `frontend/beta/fixtures.ts`. It is labelled throughout ("Epic Beta preview. Offline synthetic data only…", "Synthetic
  data", "Synthetic MOCK patient"). Questions are unavailable. At runtime it makes no data requests and uses no cookies
  or storage; the bundle still contains the unused HTTP client.
- **Guards.**
  - The same branch and published-tip rules, account check, code scan, headers, smoke retries and rollback apply.
  - It refuses any copy and writes no record.
  - The build must carry both the synthetic banner and the invented fixtures, and no publication loader. CI checks the
    real fixture build the same way.
  - Its smoke test requires the record path to return 404.
- **Epic-copy path unchanged.** Every check on the Epic-copy path stays the same.
- **Later swap.** Publishing the approved Epic copy later is a normal release to the same Worker, with automatic
  rollback to the demo.
- **Not a takedown.** A failed demo release rolls back automatically to whatever was live before it, which could be an
  Epic copy. To remove an Epic copy (for example if Epic withdraws permission), use step 5, `wrangler delete`, not the
  demo.

## Verification (offline)

- **Python:** publication transform and export launcher tests, with invented fixtures only. They cover:
  - every synthetic-marker mismatch;
  - path, junction, Git work tree and root-overlap refusals;
  - the environment lock and attended-console gate, which is tested on Windows.
- **Frontend:** published-mode validator and page tests. A copy is accepted only when every synthetic marker agrees
  with its origin. Invented fixtures are accepted only by a local preview build (`VITE_BETA_ALLOW_FIXTURE=true`); the
  hosted build refuses them.
- **Edge:** run the Beta release tests with `node --test test/release-beta.mjs` from `deploy/cloudflare`. They cover
  each refusal with no remote step, smoke retries and rollback, and confirm the real deploy folder is never written.
  The Stable edge's `npm run check` does not include them, because the Stable edge tooling stays unchanged.
- **Isolation:** the published build contains no owner, WebAuthn or loopback code, and the owner and MOCK builds
  contain no published-copy code.
- **Local preview:** an invented, clearly labelled synthetic copy served by Wrangler's local asset server.
  - The headers, redirects, all eight pages, phone width and network requests were checked, and no cookies or storage
    were used.
  - The default published build refused the invented copy and showed nothing.
- **Genuine owner build:** `BetaShell` gained an additive `published` flag, so a rebuild of the genuine-owner UI no
  longer matches the accepted build byte for byte. The accepted, pinned assets stay in use for any re-serve; re-pin
  only after reviewing a rebuild.
