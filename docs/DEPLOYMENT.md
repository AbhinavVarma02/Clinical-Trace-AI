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
| `CLINICAL_TRACE_LLM_PROVIDER` | `none` | `none`, `ollama`, `openai-compatible` |
| `CLINICAL_TRACE_LLM_BASE_URL` | Ollama default for `ollama` | OpenAI-compatible base URL |
| `CLINICAL_TRACE_LLM_MODEL` | `qwen2.5:1.5b` | Model name |
| `CLINICAL_TRACE_LLM_API_KEY` | — | Optional bearer token (never logged) |
| `CLINICAL_TRACE_LLM_TIMEOUT_S` | `60` | Provider request timeout |
| `CLINICAL_TRACE_RATE_LIMIT_PER_MIN` | `120` | Requests per client per minute |
| `CLINICAL_TRACE_AGENT_RATE_LIMIT_PER_MIN` | `20` | Agent runs per client per minute |
| `CLINICAL_TRACE_MAX_BODY_BYTES` | `32768` | Request body limit |
| `CLINICAL_TRACE_SKIP_DOTENV` | — | `1` to ignore a local `.env` |

## CI

`.github/workflows/ci.yml` runs: ruff lint, pytest (including the evaluation gate), the evaluation report, frontend typecheck/tests/builds, `npm audit`, a tracked-file secret scan, `pip-audit` (warning only), and a container build with a smoke test (status, workspace HTML, an answered agent run, non-root user).

## Public demo (Hugging Face Space)

The current public demo (https://huggingface.co/spaces/abhinavvathadi/clinical-trace-ai) runs the **v1** Streamlit dashboard and FastAPI from a separate Space repository with its own Dockerfile and `start.sh`. It has **not** been updated to 2.0.

Status: **blocked on owner approval** (see `docs/PROJECT_STATUS.md`). The planned path, once approved:

1. Clone the Space, port the 2.0 changes, keep the Streamlit dashboard reachable, and add the workspace at `/app` (or make it the landing page).
2. Build from an LF-normalised export, run the container smoke test locally.
3. Commit to the Space pinned to the current parent commit; keep the previous commit as the rollback point.
4. Verify from an unauthenticated browser: status, guided demos, agent runs, citations, review flow, mobile layout.

Free CPU Spaces run the deterministic mode reliably. Live LLM inference in the public demo needs either an in-container small model (measured locally at ~1 s p50 for 1.5B with poor quality, ~2.7 s for 7B on the development desktop via Ollama — latency on the free CPU Space is unmeasured) or a hosted free-tier endpoint with a secret token; both need an owner decision.

## SQLite runtime lifecycle

Use exactly one Uvicorn worker per database. Persistent storage must include the SQLite database and its WAL/SHM files during operation; use SQLite backup facilities rather than copying a live database file. No public deployment was changed. See [RELIABILITY.md](RELIABILITY.md) for explicit restart recovery, capacity and retention.
