# Dedicated Cloudflare account (2026-10-18)

The public 2.0 edge now runs on its own Cloudflare Free account, separate from the
owner's other projects. The previous edge on the original account was not changed
or deleted and still serves the same backend.

## Current public edge

| Item | Value |
| --- | --- |
| Public URL | https://clinical-trace-ai.vathadiabhinav.workers.dev |
| Account | "Vathadiabhinav@gmail.com's Account", id `00f876c0ebbf368d25feabe3e49cbc0b` (separate login; Workers plan **Free**, read in the dashboard) |
| `workers.dev` subdomain | `vathadiabhinav` (assigned by Cloudflare) |
| Worker | `clinical-trace-ai`; release `988dabc`, version `1a506827-2e55-450a-a474-c1ed693f84ac` |
| Rollback target | version `c9725bc0-98ab-4c80-ba59-ce71cfc68505` (first deployment, release `9585a3a`) |
| D1 | `clinical-trace-edge`, id `fd9f4f5c-e9f4-47d2-b95f-a8c87f9d07a2` (ENAM), migration `0001_edge_usage.sql` applied |
| Bindings | Workers AI (`@cf/qwen/qwen3-30b-a3b-fp8`), `API_LIMITER` 120/min, `AGENT_LIMITER` 6/min, static assets, daily cron |
| Limits (unchanged) | 8,000 neurons/day (platform allocation 10,000), 25 agent runs per visitor and 300 per deployment per day |
| Secrets | `EDGE_BACKEND_SECRET` (the existing backend secret, reused so the Space needed no change), `LLM_PROXY_TOKEN` (new for this account). Values only in Cloudflare and the git-ignored `deploy/cloudflare/.prod-secrets.clinical-trace.json` |
| Backend | Unchanged co-hosted Space `9afa0f8` (`BACKEND_ORIGIN` unchanged); v1 dashboard unchanged |

Previous edge, still running: https://clinical-trace-ai.room-for-both.workers.dev,
account `4d8c9b9dcf23e738359858eb1a6d9d9e`, release `475f0b6`, Worker version
`76ecd882-4084-4f71-8648-fa031337c9dc`, D1 `57896756-9a09-4aa7-a63b-7039482aa163`.
Its configuration is `deploy/cloudflare/wrangler.jsonc` as of commit `fb5c016`.

## How the account is pinned

- `wrangler.jsonc` names `account_id`; Wrangler refuses to act on it from a login
  that cannot see that account.
- Wrangler 4.149 auth profile `clinical-trace` (experimental `wrangler auth create`)
  holds the OAuth login for the new account and is bound to `deploy/cloudflare`
  only (`wrangler auth activate`). The default profile still belongs to the
  original account. No `CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID` is used.
- `scripts/release.mjs` checks `wrangler whoami --json` for the pinned account
  before building, before migrations and before deploying.
- First deployment: `node scripts/release.mjs --first-deploy --secrets-file .prod-secrets.clinical-trace.json`.
  It refuses an existing Worker. Because there is no earlier version, a failed
  first deployment is recorded (`first_deploy_failed`) and left for an operator;
  nothing is deleted automatically.
- Later releases: `npm run release` (automatic rollback to the previous version).

## Verification

Offline, before publication (no inference): 415 Python tests (also with a
deliberately misconfigured provider environment, now forced offline by
`tests/conftest.py`), locked offline gate PASS 41/42 (known `ho-013` miss),
frontend 52 tests (53 after the fix below) plus TypeScript and builds, Worker 30 tests plus 13 release
orchestration tests, 36 Playwright tests (desktop, tablet, mobile; deterministic
backend), ruff, scoped mypy and format checks, secret scan (297 files, 0 hits),
npm audits (0 vulnerabilities), deploy dry run.

Publication (owner approved "new Worker only"): first deployment `9585a3a`, then
fix release `988dabc`; baseline smoke 7/7 both times (static app, security
headers, status, metrics, route allow-list, proxy authentication, workspace
validation). A non-inference API call through the new edge returned the synthetic
patient list, confirming the reused backend secret.

**Single live guided-case run** (desktop, 2026-10-18 18:55 UTC, through the public URL):

| Check | Result |
| --- | --- |
| Model | Live LLM, `@cf/qwen/qwen3-30b-a3b-fp8`; planner and composer both on Workers AI |
| Cost | 2 Workers AI calls, **34.9 neurons** (edge accounting; 0 before, 34.9 after) |
| Answer | 8.2 s; 3 verified claims, 8 drafted claims excluded by the verifier; "Review recommended" |
| Citations | `p001-e1@v1`, `p001-e3@v1`, `p001-e4@v1`, all for the selected synthetic patient; drawer shows `Encounter/p001-e1 · version 1` |
| Coverage | The three heart-failure admissions, including the return admission 19 days after discharge (by dates; not labelled as a readmission). Metformin cessation was **not** covered: the Space still runs the backend from before the guided-completeness work |
| Review | Submitted as review `7687ef1d` (v1), approved by the reviewer persona; approval bound to the artifact hash |
| Mobile (375 px) | Same approved artifact displayed without rerunning; no horizontal overflow |
| Isolation | Own workspace lists 1 review; a random workspace lists 0; direct access from it returns 404 |

The run exposed a defect: switching to the reviewer remounted the agent page and
re-fired the guided `run=1` link as the reviewer. The backend refused it (403, no
model call), but it counted against the visitor's daily allowance. Fixed in
`988dabc` (the trigger is consumed when the run starts) with a regression test,
and released with approval. The fix was verified in tests and in the served
bundle, not by a second live run.

Migration totals: **1 live agent run, 2 inference calls, 34.9 neurons** (target 100,
ceiling 150). No 42-case cloud evaluation, retries or paid services. One smoke
run establishes basic live function, not statistical reliability.

After publication: original edge `475f0b6` ready, v1 health and page 200, Space
`9afa0f8` RUNNING on cpu-basic.

## Remaining limitations

- The newer backend (`be52df7`, verified locally, staged in a git-ignored folder)
  is not published. The live agent therefore lacks the guided-completeness and
  reliability improvements, including metformin-cessation coverage. Publishing
  it changes the Space shared by both edges and needs separate approval.
- The backend runs on free Hugging Face hosting that sleeps when idle; there is
  no uptime guarantee and the 48-hour wake time is unmeasured.
- Clinical validation and independent human-review agreement remain unmeasured.
