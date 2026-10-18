# Coordinated release checklist

Prepared 2026-10-18. **No deployment or merge is currently authorized.** Commands
under publication/rollback are for an explicitly approved future release.

## Pins and current readiness

| Artifact | Pin / evidence |
| --- | --- |
| Existing edge | source `475f0b6`; Worker version `76ecd882-4084-4f71-8648-fa031337c9dc` (100% deployment, read 2026-10-18) |
| Existing co-hosted Space | `9afa0f8dfe602f42a8a7de3b7ac328da08966de9`, RUNNING on `cpu-basic` |
| Locally verified backend candidate | source `be52df7357d1746d3704b6a140893e4ec2094656`, Space parent pinned above; ignored `.project-state/cohost-release-be52df7/` |
| Local co-host verification | Docker image `sha256:54511eb5e8d0e9ff1e7310f607cd7e5b1f91490fc7339e5d9999dec7c966c92c`, 9/9; nginx/API ready in 16.5 s; v1 UI/API, public access rules, streamed deterministic run, model and review approval passed |
| Full v1-only emergency restoration | `380103e1d89e6c7a4049256fa63f9240d5c6653d` (historically verified identical tree to `91e7685`) |

Stage preserves the Space's `dashboard/`, models, README and configuration;
the backend/frontend overlay comes from Git, excluding local private files.
No v1 dashboard/component source or trained model was changed by this work.
`be52df7` contains the verified backend and its bundled workspace. The separately
built edge workspace also includes the later section-retry recovery fix (52
frontend tests). Backend/v1 code and edge/backend contracts remain the same;
release-tool and documentation changes do not change the verified backend code.

**Still blocked:** a fresh full locked real-model evaluation and a fresh full
public LLM workflow. The protected reserve prevents inference in this session.
The existing production version's historical full public gate is not a new
measurement of this candidate. Full 48-hour sleep/wake time, independent human
agreement and clinical validation remain unmeasured.

## Before requesting publication approval

- Confirm the intended edge commit is pushed on `feature/clinical-trace-2.0`
  and all five head-specific CI jobs pass. Keep PR #1 open/unmerged.
- Recheck `/edge/status` once. Fund the unchanged 42-case gate only from a new
  declared free allocation above the 2,000-neuron reserve; otherwise leave it
  **PENDING — QUOTA BLOCKED**. Do not consume reserve or replay old experiments.
- Inspect the local co-host manifest and verification report; re-stage/re-verify
  if backend/frontend code or the Space parent changes. Keep failed reports.
- Confirm account remains **Workers Free**, Space remains **cpu-basic**, neuron
  cap remains **8,000**, run caps remain **25/connection/day and 300/global/day**,
  and no paid billing/gateway or persistent paid storage is configured.
  Cloudflare plan confirmation is historical owner evidence; this session did
  not independently read the account's billing subscription.
- Reserve free quota for the post-publication full smoke. Its real planner and
  composer must run; deterministic degradation now fails the full release gate.
- Capture the exact current Worker version and Space parent again immediately
  before publication. Prefer the current full co-host revision for rollback;
  the v1-only emergency target removes the 2.0 API.

Safe checks (from repository root; Windows venv Python shown):

```bash
git branch --show-current
git rev-parse HEAD
git status --short
venv/Scripts/python.exe -m pytest tests/ -q --basetemp=C:/Users/devik/AppData/Local/Temp/ct-release-check -W ignore -p no:cacheprovider
venv/Scripts/python.exe -m ruff check .
venv/Scripts/python.exe -m evaluation.run_eval --no-write
npm --prefix frontend run check
npm --prefix frontend run test:e2e
npm --prefix deploy/cloudflare run check
npm --prefix deploy/cloudflare run deploy:dry-run
node deploy/cloudflare/scripts/smoke.mjs https://clinical-trace-ai.room-for-both.workers.dev --release 475f0b6
```

The browser suite starts an owned disposable real backend and runs in
deterministic mode, with simulated edge-limit responses for quota scenarios.
It verifies desktop, tablet and mobile flows, citations, patient/persona/workspace
isolation, review approval and queued cold-start recovery. Worker relay tests
exercise protocol compatibility without spending inference quota.

## Publication — only after explicit approval

Use the same pinned candidate code for both components. If rebuilding the backend
from a newer commit, use a new local stage path and verify it before publishing:

```bash
venv/Scripts/python.exe deploy/huggingface/space_cohost.py stage --out C:/Users/devik/Desktop/ct-release-stage --revision 9afa0f8dfe602f42a8a7de3b7ac328da08966de9
venv/Scripts/python.exe deploy/huggingface/space_cohost.py verify --out C:/Users/devik/Desktop/ct-release-stage
venv/Scripts/python.exe deploy/huggingface/space_cohost.py publish --out C:/Users/devik/Desktop/ct-release-stage --i-have-owner-approval
cd deploy/cloudflare
npm run release -- --full
```

`stage` replaces its output directory: choose a dedicated new path. `publish`
refuses a moved Space parent. Backend settings must remain public mode + edge
secret + `cloudflare-workers-ai-relay` + the existing Qwen3 model. The Space
cannot call Cloudflare directly; inference relay frames travel backend → edge,
the edge POSTs results to the backend, and no relay frame reaches the browser.
Non-streamed execution does not establish real inference.

Inspect the full smoke JSON for actual LLM planning/composition, verified
same-patient/version citations, cross-workspace 404, and hash-bound review
approval. Verify v1 `/_stcore/health` and UI still respond. Recheck quota, desktop
and mobile UI, new visitor/global counters, UTC reset and temporary limit copy.

## Rollback expectations

The release runner requires one known previous Worker version before any
migration or publication. It writes `.wrangler/last-release.json`. Migration
failure stops before edge deploy (`migration_failed`). A deploy error or failed
smoke triggers `wrangler rollback` to the captured version and returns failure;
the receipt says `rolled_back` only if that command succeeds. Failure of rollback
is explicit (`rollback_failed`); an operator must act. Six isolated orchestration
tests cover these paths; no new live rollback drill was performed.

**Automatic rollback covers only the Worker. It does not undo a Space publication
or D1 migration.** This change adds no schema migration. Require backward-compatible
migrations; stop for manual recovery if rollback fails. A Space publish can leave
the new backend running while the edge rolls back. Restore both pins when a
coordinated gate fails, then run the read-only baseline and an affordable full
gate after recovery.

Approved full-cohost restoration to the prior production revision:

```bash
npx wrangler rollback 76ecd882-4084-4f71-8648-fa031337c9dc --message "restore known-good edge" -y
# From repository root; replace NEW_SPACE_SHA with the failed publication's exact SHA:
venv/Scripts/python.exe deploy/huggingface/space_cohost.py restore-cohost --revision 9afa0f8dfe602f42a8a7de3b7ac328da08966de9 --expected-current-revision NEW_SPACE_SHA --i-have-owner-approval
```

Run the Worker command from `deploy/cloudflare` and the Python command from the
repository root. `restore-cohost` makes one atomic HF commit pinned to the failed
publication, restores every selected repository file, deletes added files, and
leaves existing relay settings/secrets/hardware unchanged. It refuses a moved
parent or v1-only target. Six mocked tests cover atomic restoration, races,
approval and missing parent; no live restoration was performed.

The older `rollback` command **removes 2.0 secrets/settings** and is exclusively
the v1-only emergency path. Do not use it to restore `9afa0f8`. If full-cohost
restoration is unavailable, use this exact tested emergency target and disconnect
the edge backend until a coordinated release is approved:

```bash
venv/Scripts/python.exe deploy/huggingface/space_cohost.py rollback --i-have-owner-approval --revision 380103e1d89e6c7a4049256fa63f9240d5c6653d
# Then explicitly set BACKEND_ORIGIN empty in the reviewed edge configuration.
cd deploy/cloudflare
npm run release
```

Do not run rollback as a verification drill without deployment approval. HF
SQLite is ephemeral: code rollback cannot recover runs/reviews lost on restart.

## $0 constraints

Current official [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
retains 10,000 free neurons/day and a 00:00 UTC reset; Free fails requests beyond
its allocation. The application cap stays at 8,000 and its 2,000 reserve is
additional experiment discipline. [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/)
and [HF Spaces resources](https://huggingface.co/docs/hub/spaces-overview)
describe the free tiers. These documents establish platform terms, not the
account's current billing subscription. Do not change plan, hardware, credentials,
model/provider or limits as part of this release.
