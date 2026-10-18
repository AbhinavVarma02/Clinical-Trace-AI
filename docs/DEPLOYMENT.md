# Deployment

## Local development

```bash
python -m venv .venv && .venv\Scripts\activate          # Windows (use source .venv/bin/activate on Linux/WSL2)
pip install -r requirements.txt -r requirements-dev.txt
python src/train.py                                      # optional: readmission model artifacts (downloads UCI data)
cd frontend && npm ci && npm run build:workspace && cd ..
uvicorn api.main:app --port 8000                         # workspace at http://localhost:8000/app/
```

Frontend hot reload: `cd frontend && npm run dev:workspace` (Vite on port 3002, proxies `/api` and `/fhir` to port 8000).

The SQLite database is created at `var/clinical_trace.db` (override with `CLINICAL_TRACE_DB_PATH`). The synthetic fixture is ingested idempotently on every start.

### Live LLM inference (optional, free, local)

```bash
ollama pull qwen2.5:7b
set CLINICAL_TRACE_LLM_PROVIDER=ollama               # export on Linux/WSL2
set CLINICAL_TRACE_LLM_MODEL=qwen2.5:7b
uvicorn api.main:app --port 8000
```

Any OpenAI-compatible endpoint works with `CLINICAL_TRACE_LLM_PROVIDER=openai-compatible`, `CLINICAL_TRACE_LLM_BASE_URL`, `CLINICAL_TRACE_LLM_MODEL`, and optionally `CLINICAL_TRACE_LLM_API_KEY`. Without a provider the agent runs in deterministic mode and labels every answer accordingly.

## Container

```bash
docker build -t clinical-trace-ai .
docker run -p 8000:8000 clinical-trace-ai
```

The image is multi-stage: a Node stage builds `frontend/dist` and `frontend/dist-workspace`; the runtime is `python:3.12-slim`, runs as non-root `appuser`, sets `CLINICAL_TRACE_SKIP_DOTENV=1`, and has a health check on `/api/v2/system/status`. Readmission model artifacts are included only if `models/` contains them at build time; otherwise risk tools report the model as unavailable.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLINICAL_TRACE_DB_PATH` | `var/clinical_trace.db` | SQLite file (`:memory:` for tests) |
| `CLINICAL_TRACE_LLM_PROVIDER` | `none` | `none`, `ollama`, `openai-compatible`, `cloudflare-workers-ai` |
| `CLINICAL_TRACE_LLM_BASE_URL` | Ollama default for `ollama` | OpenAI-compatible base URL |
| `CLINICAL_TRACE_LLM_MODEL` | `qwen2.5:1.5b` | Model name |
| `CLINICAL_TRACE_LLM_API_KEY` | — | Optional bearer token (never logged) |
| `CLINICAL_TRACE_LLM_TIMEOUT_S` | `60` | Provider request timeout |
| `CLINICAL_TRACE_RATE_LIMIT_PER_MIN` | `120` | Requests per client per minute |
| `CLINICAL_TRACE_AGENT_RATE_LIMIT_PER_MIN` | `20` | Agent runs per client per minute |
| `CLINICAL_TRACE_MAX_BODY_BYTES` | `32768` | Request body limit |
| `CLINICAL_TRACE_SKIP_DOTENV` | — | `1` to ignore a local `.env` |
| `CLINICAL_TRACE_PUBLIC_MODE` | — | `1` requires an `X-Demo-Workspace` header and isolates visitors |
| `CLINICAL_TRACE_EDGE_SECRET` | — | When set, `/api/v2` (except status) and `/fhir` accept only requests carrying it |

## CI

`.github/workflows/ci.yml` runs: ruff lint, pytest (including the evaluation gate), the evaluation report, frontend typecheck/tests/builds, `npm audit`, a tracked-file secret scan, `pip-audit` (warning only), and a container build with a smoke test (status, workspace HTML, an answered agent run, non-root user).

## Public deployment

The 2.0 public edge (React workspace, guarded gateway, Workers AI inference) runs on Cloudflare Workers Free at https://clinical-trace-ai.room-for-both.workers.dev. The public v1 Space (https://huggingface.co/spaces/abhinavvathadi/clinical-trace-ai) is unchanged. The public 2.0 backend host awaits owner approval. Architecture, controls, measured results, release/rollback runbooks and the co-hosting plan are in [PUBLIC_DEPLOYMENT.md](PUBLIC_DEPLOYMENT.md).

Public-mode backend settings: `CLINICAL_TRACE_PUBLIC_MODE=1` (workspace header required), `CLINICAL_TRACE_EDGE_SECRET` (protected routes accept only the edge), `CLINICAL_TRACE_LLM_PROVIDER=cloudflare-workers-ai` with `CLINICAL_TRACE_LLM_BASE_URL=<edge>/edge/llm/v1` and `CLINICAL_TRACE_LLM_API_KEY` (the edge's backend token).

## SQLite runtime lifecycle

Use exactly one Uvicorn worker per database. Persistent storage must include the SQLite database and its WAL/SHM files during operation; use SQLite backup facilities rather than copying a live database file. No public deployment was changed. See [RELIABILITY.md](RELIABILITY.md) for explicit restart recovery, capacity and retention.
