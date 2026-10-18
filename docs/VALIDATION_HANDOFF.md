# Reviewer-ready deployment and validation handoff

**Updated 2026-10-18.** Branch `feature/clinical-trace-2.0`; pin `git rev-parse HEAD` before validating.

## State

- Public 2.0 demo: https://clinical-trace-ai.room-for-both.workers.dev (Cloudflare Workers Free edge, release `7012e8e`).
- Backend: co-hosted in the existing Space (commit `9afa0f8`), owner-approved; the v1 Streamlit dashboard is unchanged at https://huggingface.co/spaces/abhinavvathadi/clinical-trace-ai.
- Inference: Workers AI Qwen3-30B-A3B, relayed by the edge during streamed runs because Spaces cannot reach Cloudflare.
- Final release gate met: complete guided workflow through the public URL on desktop and mobile, plus a cold start. Evidence: [PUBLIC_DEPLOYMENT.md](PUBLIC_DEPLOYMENT.md#public-validation-2026-10-18-unauthenticated).

## Operating it

- Edge release: `cd deploy/cloudflare && npm run release -- --full` (clean tree, D1 migrations, deploy, full public smoke, automatic rollback).
- Backend update: `space_cohost.py stage --revision <current Space sha>`, `verify` (9 checks), `publish --i-have-owner-approval`, then the edge release.
- Rollback: `space_cohost.py rollback --i-have-owner-approval --revision 380103e1d89e6c7a4049256fa63f9240d5c6653d` (exact v1 tree), empty `BACKEND_ORIGIN`, edge release.
- Health: `/edge/status` and `/edge/metrics`. Free limits: 8,000 neurons/day, 300 agent runs/day, 25 per visitor.

## Still open

- Wake time after the Space's 48-hour sleep (needs 48 idle hours to measure).
- Human-review agreement needs real independent annotations ([REVIEW_VALIDATION_PROTOCOL.md](REVIEW_VALIDATION_PROTOCOL.md)). No clinical validation exists.
- Merge to `main` awaits owner approval. Conditional advanced extensions remain in the [roadmap](IMPLEMENTATION_ROADMAP.md).
