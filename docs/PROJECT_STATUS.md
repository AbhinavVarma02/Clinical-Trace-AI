# Project Status

**Version:** 2.0.0-dev on branch `feature/clinical-trace-2.0` (branched from `main` at `ecd1973`). Pushed to GitHub for CI; not merged into `main`; not deployed.
**Updated:** 2026-10-17

## Summary

| Area | State |
| --- | --- |
| v1 readmission pipeline, API, Streamlit dashboard, SHAP explorer | Unchanged and passing (54 original tests) |
| Synthetic FHIR data layer + mock FHIR server | Implemented, tested |
| Clinical Case Review Agent (LangGraph) | Implemented, tested; deterministic and local-LLM modes measured |
| Hybrid retrieval | Implemented, benchmarked |
| Safety, policy, review, audit | Implemented, tested, review flow checked in the browser |
| Evaluation harness + CI gate | Implemented; deterministic gate passing |
| React workspace (`/app`) | Implemented, unit-tested, checked in the browser (desktop, mobile width, light and dark) |
| CI workflow, hardened Dockerfile | CI triggered by the branch push (first run's result: see GitHub Actions); container not built locally (Docker daemon not running) |
| Public Hugging Face demo | Still v1; update **blocked** on owner approval |
| ML enhancements (P5), advanced extensions (P13) | Not started |

Detailed task status: [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md).

## Baseline (Phase 0, before changes)

- Git: `main` at `ecd1973`, clean apart from four intentionally untracked private documents.
- Python: `pytest tests/` → **54 passed** (Python 3.13 venv).
- Frontend: `npm run check` → typecheck OK, **24 passed**, build OK.
- Model: XGBoost `20260703051249-local`, held-out test ROC-AUC 0.683, PR-AUC 0.240, recall 0.668, precision 0.170 at threshold 0.30.
- Placeholders found: `src/database.py`, `src/monitoring.py`, `src/rag_pipeline.py`, `monitoring/drift_report.py` (one-line stubs; still stubs — the 2.0 features live in new packages).
- Deployment: the HF Space is a separate repository with its own Dockerfile/`start.sh` and an HTTP-calling dashboard fork.

## Current checks (latest run)

| Check | Result |
| --- | --- |
| `pytest tests/ -q` | 183 passed (54 original + 129 new, including the evaluation gate) |
| `ruff check .` | passed |
| `npm run check` (frontend) | typecheck OK; 32 tests passed; both builds OK |
| `python -m evaluation.run_eval` | gate passed; 41/42 cases; 0 isolation / authorisation violations |
| Local LLM evaluation | 7B gate passed (correctness 0.739); 1.5B gate failed (0.478) |

## Blockers requiring the owner

| ID | Blocker | Needed |
| --- | --- | --- |
| BLK-001 | Updating the public Hugging Face Space would overwrite the working v1 demo | Approval to deploy (and to keep or retire the Streamlit landing page) |
| BLK-002 | Live LLM inference in the public demo | Decision: in-container small model (slow/weak on free CPU) or a free hosted endpoint with a token stored as a Space secret |
| BLK-003 | Container not built or smoke-tested locally; first CI run unconfirmed | Start Docker Desktop locally and build; review the first GitHub Actions run |

## Pending tasks (not started)

| ID | Task |
| --- | --- |
| CT-P05-T01 / T03 | Readmission model calibration (Brier, ECE, reliability bins, threshold sweep) and subgroup/error analysis, without retraining |
| CT-P07-T02 | Playwright end-to-end smoke tests for the four guided demos, safety probe, persona scoping, review approval, mobile, themes |

Other open roadmap items: P5 drift monitoring, durable checkpoints, streaming, reranking, PostgreSQL/pgvector, OpenTelemetry, image-size measurement, Hugging Face deployment, and P13 extensions — see the roadmap.

## How to resume

```bash
git checkout feature/clinical-trace-2.0
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -q
python -m evaluation.run_eval
cd frontend && npm ci && npm run check
```

Then continue with the "Next executable tasks" in the roadmap.
