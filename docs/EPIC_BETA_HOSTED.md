# Hosted Epic Beta: read-only published copy

**Status (2026-11-14): implemented and tested offline only. Nothing is published.** The owner is holding publication
until Epic confirms that its developer-sandbox test-patient data may be shown on a public demonstration site. Every
deployment step below needs its own explicit owner approval.

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
4. **Link from Stable.** The Stable Beta entry is built only with `VITE_ENABLE_BETA_LINK=true`, and it currently points
   to the local owner origin. Pointing it at the hosted URL is a reviewed change to `main` followed by a normal Stable
   release (`npm run release`, with automatic rollback).
5. **Remove.** From `deploy/cloudflare`, run `npx wrangler delete --config beta/wrangler.jsonc`, then revert the
   Stable link with a normal Stable release.

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
