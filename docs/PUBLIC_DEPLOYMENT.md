# Public deployment (2.0)

**Updated:** 2026-10-18 · **Status:** edge and live inference are public; the backend host is **pending owner approval**, so the public full agent workflow is **not yet available** and the release gate is open. Decision record: [ADR 0007](ADR/0007-public-edge-and-inference.md).

| Component | Where | State |
| --- | --- | --- |
| React workspace, gateway, status/metrics | https://clinical-trace-ai.room-for-both.workers.dev (Cloudflare Workers Free) | Live, release `a18a3ea` |
| Live LLM inference | Workers AI `@cf/qwen/qwen3-30b-a3b-fp8` behind `/edge/llm/v1` (backend token only) | Live; 8,000-neuron daily cap below the 10,000 free allocation |
| Usage accounting | D1 `clinical-trace-edge` (daily counters, privacy-safe events) | Live |
| Python FastAPI / LangGraph / SHAP backend | Not public. Tested locally and in CI | **Blocked:** host decision (below) |
| v1 Streamlit demo | https://huggingface.co/spaces/abhinavvathadi/clinical-trace-ai | Unchanged at `91e7685` |

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
Backend (public mode) ──> /edge/llm/v1 for planner and composer calls
```

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

## Measured results

All figures are real measurements; scopes are stated.

| Measurement | Result | Scope |
| --- | --- | --- |
| Public page load | TTFB 126-239 ms; first visit load 0.48 s, 107 KB transferred; warm reload 89 ms | Built-in Chromium against the live URL, 2026-10-18 |
| Public inference proxy | 61 calls, p50 476 ms, p95 4.09 s, 0 provider errors, 0 quota rejections, 350 neurons | Live `/edge/metrics` |
| Locked 42-case evaluation via the deployed proxy | Gate PASS, 37/42, correctness 0.870, unsupported-claim rate 0.049, p50 1.32 s, p95 5.00 s | Local backend → public edge → Workers AI (exploratory, not written) |
| Recorded Workers AI evaluation | Gate PASS, 36/42, correctness 0.826, unsupported-claim rate 0.144, p50 1.08 s, p95 3.90 s, 358.5 neurons | `evaluation/results/llm_cf_qwen_qwen3-30b-a3b-fp8.json` at `759e55c` |
| Full chain, cold start included | Queued guided question sent automatically when the backend became ready; answered with live LLM, 9/9 claims verified (5.67 s); citation, review and approval completed | Local edge → co-hosted container → deployed inference proxy, in the browser |
| Full chain scripted smoke | 14/14 checks; streamed run 6.74 s, 13 progress frames, 5 tools, 9 claims, LLM planner and composer; cross-workspace read 404 | `deploy/cloudflare/scripts/smoke.mjs --full` against the local edge |
| Container image | 2.01 GB | Docker Desktop, LF export + trained models |
| Backend cold start and memory (2 vCPU / 4 GB) | Ready in 16.6 s; peak 290 MiB; risk+SHAP 0.68 s; warm runs 45-277 ms | `evaluation/results/container_resources.json` |
| Backend cold start and memory (0.1 vCPU / 512 MB) | Ready in 248.8 s; peak 296 MiB, no OOM; risk+SHAP 6.9 s; warm runs 0.3-1.5 s | Same; approximates a 512 MB free container host |
| Co-hosted Space image | 2.0 API ready through nginx in 13.7 s; 9/9 checks incl. unchanged v1 dashboard | `deploy/huggingface/space_cohost.py verify` |
| Rollback drill | Previous Worker live in 5 s, roll-forward in 7 s, smoke passed | `wrangler rollback` on the live edge |

Not yet measurable: public agent completion time, public error rates and real cold-start recovery, because no public backend exists. `/edge/metrics` will report them from the first public runs.

## Backend host options (owner decision)

Hugging Face now requires a paid plan to create Docker or Gradio Spaces, so a separate free 2.0 Space is impossible. No-card container hosts (Render, SnapDeploy, Railway credit, Back4App) cap at 512 MB / 0.1-0.25 vCPU, sleep after about 15 minutes and require an owner account; always-free compute with more resources requires a payment card.

| Option | Fit | Cost / risk |
| --- | --- | --- |
| **A. Co-host in the existing Space (recommended)** | 2 vCPU / 16 GB; measured 13.7-16.6 s start; sleeps after 48 h idle | Modifies the v1 deployment: v1 dashboard stays at `/` unchanged (verified), 2.0 API added at `/api/v2` behind nginx. Rollback is one command to `91e7685` |
| B. Render free web service | Functionally verified at 512 MB / 0.1 vCPU (no OOM) | About 4 min start plus platform wake after every 15 idle minutes; ephemeral SQLite; owner must create the account and publish an image containing the models |
| C. No backend | Edge, evidence and inference only | Release gate stays open |

### Co-hosting runbook (only after explicit approval)

```bash
python deploy/huggingface/space_cohost.py stage  --out C:/Users/<you>/AppData/Local/Temp/ctch   # short path on Windows
python deploy/huggingface/space_cohost.py verify --out C:/Users/<you>/AppData/Local/Temp/ctch   # docker build + 9 checks
python deploy/huggingface/space_cohost.py publish --out C:/Users/<you>/AppData/Local/Temp/ctch --i-have-owner-approval
# then set BACKEND_ORIGIN=https://abhinavvathadi-clinical-trace-ai.hf.space in wrangler.jsonc vars, commit, and:
cd deploy/cloudflare && npm run release -- --full
```

`publish` refuses to run if the Space moved from the staged parent revision, sets the edge secret and inference token as Space secrets (never files), and commits with the parent pinned. **Rollback:** `python deploy/huggingface/space_cohost.py rollback --i-have-owner-approval` restores the exact `91e7685` tree, removes added files, secrets and variables; set `BACKEND_ORIGIN` back to empty and release the edge.

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

The public demo is **not** ready. Ready requires: an approved public backend, `npm run release -- --full` passing against the public URL (guided workflow, citations, isolation, review approval), desktop and mobile verification by an unauthenticated browser, and `/edge/metrics` showing real public agent completion and error rates.
