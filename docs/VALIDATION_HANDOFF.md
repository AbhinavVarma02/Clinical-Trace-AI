# Reviewer-ready deployment and validation handoff

**Updated 2026-10-18.** Branch `feature/clinical-trace-2.0`; pin `git rev-parse HEAD` before validating. The public v1 Space is unchanged at `91e7685`.

## Done in this phase

- Cloudflare edge live on Workers Free: https://clinical-trace-ai.room-for-both.workers.dev (static workspace, guarded `/api/v2` gateway, `/edge/status`, `/edge/metrics`, Workers AI proxy, D1 accounting). Release with `cd deploy/cloudflare && npm run release`; it refuses dirty sources, applies D1 migrations, deploys, smoke-tests and rolls back automatically on failure.
- Free inference selected by measurement: Workers AI Qwen3-30B-A3B (recorded gate pass 36/42). See [EVALUATION.md](EVALUATION.md#hosted-free-inference-cloudflare-workers-ai-2026-10-18).
- Backend public mode: per-browser workspaces isolate visitors; edge secret restricts the backend to edge traffic; quota exhaustion degrades to labelled deterministic answers.
- Workspace: availability indicator, cold-start queueing and recovery, guided four-step case, static evidence snapshot.
- Local Docker measurements and a tested, reversible co-hosting package for the existing Space.
- Full chain verified locally: edge → co-hosted container → deployed inference proxy, including a real cold start, in the browser and with `smoke.mjs --full` (14/14).

## Release blocker and exact next steps

The public backend host needs an owner decision ([options](PUBLIC_DEPLOYMENT.md#backend-host-options-owner-decision)). After explicit approval of co-hosting:

1. `python deploy/huggingface/space_cohost.py stage --out <short path>` then `verify` (expects 9/9).
2. `python deploy/huggingface/space_cohost.py publish --out <path> --i-have-owner-approval` (pinned parent `91e7685`; secrets set as Space secrets).
3. Set `BACKEND_ORIGIN` to `https://abhinavvathadi-clinical-trace-ai.hf.space` in `deploy/cloudflare/wrangler.jsonc`, commit, `npm run release -- --full`.
4. From a fresh unauthenticated browser on desktop and mobile: guided case, citations with versions, review approval, cross-visitor isolation, cold start after the Space sleeps, theme and keyboard checks. Record `/edge/metrics` (agent completion p50/p95, availability error rate, cold-start recovery).
5. Only then mark the public demo ready and update the README.

Rollback at any point: `python deploy/huggingface/space_cohost.py rollback --i-have-owner-approval`, empty `BACKEND_ORIGIN`, `npm run release`.

## Still open beyond the release gate

Human-review agreement needs real independent annotations ([REVIEW_VALIDATION_PROTOCOL.md](REVIEW_VALIDATION_PROTOCOL.md)). No clinical validation exists. Conditional advanced extensions remain in the [roadmap](IMPLEMENTATION_ROADMAP.md). Private continuity records live in the ignored `.project-state/`.
