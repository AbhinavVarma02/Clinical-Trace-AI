# Public deployment (2.0)

**Updated:** 2026-10-18 · **Status: public demo operational.** The final release gate passed through the unauthenticated public URL (full guided workflow with live LLM inference, citations, isolation, review approval) on desktop and mobile, including a backend cold start. Decision record: [ADR 0007](ADR/0007-public-edge-and-inference.md).

| Component | Where | State |
| --- | --- | --- |
| React workspace, gateway, status/metrics | https://clinical-trace-ai.room-for-both.workers.dev (Cloudflare Workers Free) | Live, release `7012e8e` |
| Live LLM inference | Workers AI `@cf/qwen/qwen3-30b-a3b-fp8`, relayed by the edge during streamed runs | Live; 8,000-neuron daily cap below the 10,000 free allocation |
| Python FastAPI / LangGraph / SHAP backend | Co-hosted in the existing Space (owner-approved): nginx routes `/api/v2` to FastAPI, everything else to the unchanged v1 Streamlit dashboard | Live, Space commit `9afa0f8`; rollback target `380103e` (identical tree to `91e7685`) |
| Usage accounting | D1 `clinical-trace-edge` | Live |
| v1 Streamlit demo | https://huggingface.co/spaces/abhinavvathadi/clinical-trace-ai | Same URL and behaviour as before |

## Architecture

```
Browser ──> Cloudflare edge (Workers Free)
            ├─ /app/*            static React workspace (no Worker invocation, CSP + HSTS)
            ├─ /edge/status      backend probe, inference budget, daily limits
            ├─ /edge/metrics     aggregate request/error/latency/neuron/cold-start counts (D1)
            ├─ /api/v2/*         allow-listed gateway: salted per-day client key, rate limits,
            │                    daily agent-run caps, 32 KB bodies, typed cold-start errors
            │                    ──> backend (X-Edge-Secret, X-Edge-Client, X-Demo-Workspace)
            └─ /edge/llm/v1/chat/completions  Workers AI proxy, backend token, neuron budget
Backend (public mode, relay provider): emits `event: relay` frames on the streamed run;
the edge runs each completion on Workers AI and POSTs it to /api/v2/agent/relay/{id}.
```

**Why a relay.** Hugging Face Spaces cannot open connections to Cloudflare endpoints (observed 2026-10-18: zero requests from the Space reached the Worker; public forum reports describe the same DNS failures and TLS timeouts). The first co-hosting attempt therefore failed the release gate and was rolled back. Inbound traffic (edge to Space) works, so the edge performs inference during the streamed request and returns the result to the waiting backend call. The Space holds no inference URL or token, and relay frames never reach the browser. Non-streamed requests cannot relay and answer in labelled deterministic mode.

The browser never holds a provider key. Each browser creates a random 128-bit workspace id; the backend stores ownership as `persona@sha256(workspace)[:20]`, so runs, sessions, reviews, feedback and audit events are invisible to other visitors. No IP address leaves the edge or is stored; per-visitor limits use a SHA-256 of the IP with a secret salt that rotates daily.

## Zero-cost and abuse controls

| Control | Value | On exhaustion |
| --- | --- | --- |
| Workers requests | 100,000/day (platform) | Platform error 1027; static assets are not counted |
| Workers AI | 8,000 neurons/day (edge cap; platform allocation 10,000) | `429 quota_exhausted`; backend answers in labelled deterministic mode until 00:00 UTC |
| Agent runs | 25 per visitor/day, 300 per deployment/day; 6 per visitor/minute | `429 daily_limit` / `rate_limited`, explained in the UI |
| General API | 120 requests per visitor/minute | `429 rate_limited` |
| Body size | 32 KB at edge and backend | `413` |
| Backend access | Requires the edge secret in public mode | `403 edge_required` |

The account is on the Workers Free plan (owner-confirmed 2026-10-18), so every platform limit fails with an error instead of a charge. Do not enable Workers Paid without revisiting ADR 0007.

## Public validation (2026-10-18, unauthenticated)

| Check | Result |
| --- | --- |
| Release gate `npm run release -- --full` at `7012e8e` | 14/14: streamed run 9.6 s, 15 progress frames, 7 tool calls, 15 verified claims, live LLM planner and composer, every citation resolved to versioned same-patient evidence, cross-visitor read 404, review submitted and approved |
| Desktop browser, fresh workspace | First progress 0.26 s; verified answer 10.9 s (15/15 claims, live LLM); citation drawer with record version; review approved ("valid for the exact artifact version") |
| Mobile 375 px, fresh workspace | Verified answer 7.5 s (15/15); treatment-advice probe refused in 14 ms before any model call; no horizontal or top-bar overflow; review approved; only this visitor's review listed |
| Cold start (Space restart) | The edge observed the outage and recovery (2.8 s recorded); the full gate started during the outage waited 5.2 s and passed against the fresh container (uptime 11 s) |
| v1 dashboard on the Space | Renders; "Model: Loaded · Inference API: Ready" |
| `/edge/metrics` (window to 01:45 UTC) | 89 gateway requests, 0 availability errors, 7 client errors (4 x 403 policy, 3 x 404); agent completion p50 9.5 s / p95 11.1 s (7 measured); 77 inference calls, p50 576 ms / p95 8.9 s, 0 provider errors, 0 quota rejections; 675 neurons used |
| First publication attempt (direct proxy) | Failed the gate (the Space could not reach Cloudflare); the edge auto-rolled back; the Space was rolled back to the exact v1 tree (verified identical) and the relay was built |

A sleeping Space (after 48 h idle) restarts its container from scratch; that wake time was not measured because it requires 48 idle hours. The workspace shows a waking state and queues the visitor's question meanwhile.

## Measured results (before publication)

All figures are real measurements; scopes are stated.

| Measurement | Result | Scope |
| --- | --- | --- |
| Public page load | TTFB 126-239 ms; first visit load 0.48 s, 107 KB transferred; warm reload 89 ms | Built-in Chromium against the live URL, 2026-10-18 |
| Public inference proxy | 61 calls, p50 476 ms, p95 4.09 s, 0 provider errors, 0 quota rejections, 350 neurons | Live `/edge/metrics` |
| Locked 42-case evaluation via the deployed proxy (before the planner/composer fix) | Gate PASS, 37/42, correctness 0.870, unsupported-claim rate 0.049, p50 1.32 s, p95 5.00 s | Local backend → public edge → Workers AI (exploratory, not written) |
| Recorded Workers AI evaluation | Gate PASS, 39/42, correctness 0.870, unsupported-claim rate 0.139, p50 1.48 s, p95 4.54 s, 434 neurons | `evaluation/results/llm_cf_qwen_qwen3-30b-a3b-fp8.json` at `d00dbc7` (36/42 at `759e55c` before the fix) |
| Full chain, cold start included | Queued guided question sent automatically when the backend became ready; answered with live LLM, 9/9 claims verified (5.67 s); citation, review and approval completed | Local edge → co-hosted container → deployed inference proxy, in the browser |
| Full chain scripted smoke | 14/14 checks; streamed run 6.74 s, 13 progress frames, 5 tools, 9 claims, LLM planner and composer; cross-workspace read 404 | `deploy/cloudflare/scripts/smoke.mjs --full` against the local edge |
| Container image | 2.01 GB | Docker Desktop, LF export + trained models |
| Backend cold start and memory (2 vCPU / 4 GB) | Ready in 16.6 s; peak 290 MiB; risk+SHAP 0.68 s; warm runs 45-277 ms | `evaluation/results/container_resources.json` |
| Backend cold start and memory (0.1 vCPU / 512 MB) | Ready in 248.8 s; peak 296 MiB, no OOM; risk+SHAP 6.9 s; warm runs 0.3-1.5 s | Same; approximates a 512 MB free container host |
| Co-hosted Space image | 2.0 API ready through nginx in 13.7 s; 9/9 checks incl. unchanged v1 dashboard | `deploy/huggingface/space_cohost.py verify` |
| Rollback drill | Previous Worker live in 5 s, roll-forward in 7 s, smoke passed | `wrangler rollback` on the live edge |


## Backend hosting and rollback

Hugging Face requires a paid plan to create new Docker Spaces, and no-card container hosts cap at 512 MB / 0.1 vCPU (measured: about 4 minutes to start), so the owner approved co-hosting in the existing Space. Re-deploy or update:

```bash
python deploy/huggingface/space_cohost.py stage  --out C:/Users/<you>/AppData/Local/Temp/ctch --revision <current Space sha>
python deploy/huggingface/space_cohost.py verify --out C:/Users/<you>/AppData/Local/Temp/ctch   # docker build + 9 checks
python deploy/huggingface/space_cohost.py publish --out C:/Users/<you>/AppData/Local/Temp/ctch --i-have-owner-approval
cd deploy/cloudflare && npm run release -- --full
```

**Rollback:** `python deploy/huggingface/space_cohost.py rollback --i-have-owner-approval --revision 380103e1d89e6c7a4049256fa63f9240d5c6653d` restores the v1 tree (verified identical on 2026-10-18) and removes the 2.0 secret and variables; then set `BACKEND_ORIGIN` to empty and run `npm run release`. The edge alone rolls back with `npx wrangler rollback <version-id>`.

## Edge operations

```bash
cd deploy/cloudflare
npm run check          # wrangler types, tsc, 19 unit tests
npm run release        # refuses dirty sources; builds workspace; applies D1 migrations; deploys; smoke; auto-rollback
npm run smoke          # baseline public checks; add -- --full once a backend is configured
npx wrangler deployments list && npx wrangler rollback <version-id> --message "reason"
```

Secrets live only in Cloudflare (`wrangler secret bulk`) and the git-ignored `deploy/cloudflare/.prod-secrets.json`. Local development uses `.dev.vars` (git-ignored). The AI binding is declared `remote: true`; without it, local `wrangler dev` routes outbound fetches remotely and cannot reach a local backend.

## Release gate

**Met on 2026-10-18** (see Public validation). Re-run `npm run release -- --full` after every edge or backend change; the release script rolls the edge back automatically if it fails.
