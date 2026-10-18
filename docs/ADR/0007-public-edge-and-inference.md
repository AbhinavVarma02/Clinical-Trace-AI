# ADR 0007: Zero-cost public edge, Workers AI inference and an open backend-host decision

**Status:** accepted for the edge and inference (2026-10-18); backend host **open** (owner decision pending)

**Context.** The 2.0 public demo must be reachable instantly, run the real Python/FastAPI/LangGraph/SHAP backend, offer live language-model inference without visitor keys, cost $0 with no billing exposure, and leave the public Hugging Face v1 Space unchanged. Verified constraints on 2026-10-17/18:

- Hugging Face now requires a paid plan to *create* Docker or Gradio Spaces; the owner account is free, so a separate free v2 Space is impossible. Existing Spaces keep running. Free ZeroGPU hosting is Gradio-only and quota-metered per visitor, which does not fit an API backend.
- Workers Free: 100,000 requests/day, 10 ms CPU per invocation, static asset requests free; limits fail with errors, not charges. Workers AI includes 10,000 neurons/day; on Free, further calls fail with an error. D1 free storage 5 GB.
- No-card container hosts (Render, SnapDeploy, Railway's $1 credit, Back4App) offer at most 512 MB RAM and 0.1-0.25 vCPU, sleep when idle and need an owner account. Card-gated always-free compute (Oracle, Cloud Run, Koyeb, Azure) is excluded by the no-billing rule.

**Decision.**

1. **Edge on Cloudflare Workers Free** (`deploy/cloudflare`): Workers Static Assets serves the React workspace without invoking code; the Worker exposes an allow-listed `/api/v2` gateway (salted per-day client keys, per-minute rate-limit bindings, per-visitor and global daily agent-run caps in D1, 32 KB bodies, typed `backend_waking`/`backend_unavailable` errors) and `/edge/status` and `/edge/metrics`.
2. **Inference via Workers AI** behind an authenticated OpenAI-compatible proxy (`/edge/llm/v1/chat/completions`). The backend's existing `OpenAICompatibleProvider` calls it; no visitor keys. A D1 neuron budget (8,000/day, hard-capped at 9,500) refuses calls before the platform's 10,000 allocation; platform error 4006 is mapped to `quota_exhausted`. The backend then fails fast without retries and labels answers as deterministic fallback until 00:00 UTC.
3. **Model: `@cf/qwen/qwen3-30b-a3b-fp8`** with its documented `/no_think` switch. Locked 42-case comparison through the same proxy (exploratory runs, gate passed for all): Qwen3-30B-A3B 37/42, correctness 0.870, p50 1.07 s / p95 3.90 s, about 9 neurons per run; Llama 3.3 70B 37/42, 0.826, 3.70 s / 10.7 s; Gemma 4 26B 37/42, 0.783, 9.2 s / 20.5 s. The recorded run at `759e55c` scored 36/42, correctness 0.826, unsupported-claim rate 0.144: hosted inference is not bit-reproducible at temperature 0, so the recorded run, not the best run, is the evidence.
4. **Backend public mode** (`CLINICAL_TRACE_PUBLIC_MODE=1`, `CLINICAL_TRACE_EDGE_SECRET`): every visitor has an opaque random workspace; sessions, runs, reviews, feedback and audit events are stored under `persona@sha256(workspace)[:20]` and are invisible across workspaces. Protected routes accept only edge traffic.
5. **Backend host: not decided.** Keep v1 unchanged. Either an independent free host proves it can run the full workflow within its limits, or the owner explicitly approves the reversible co-hosting plan in `docs/PUBLIC_DEPLOYMENT.md`.

**Consequences.** The workspace, measured evidence and live inference are public at $0; the full agent workflow is not public until the backend host is resolved, so the release gate remains open. Hosted-model quality varies run to run and is reported as measured. The Workers Free plan is the cost control: no paid plan may be enabled on the account without revisiting this ADR.
