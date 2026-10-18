# Architecture

Clinical-Trace AI 2.0 is a **modular monolith**: one FastAPI process serves the v1 readmission API, the v2 clinical workspace API, a read-only mock FHIR server, and the compiled React workspace. Separate services were not introduced because nothing yet needs independent scaling, and a single container fits the free hosting tier (see [ADR 0001](ADR/0001-modular-monolith.md)).

```text
                React + TypeScript workspace  (/app, static build)        Streamlit dashboard (v1, unchanged)
                               |                                                  |
                               v                                                  v
        FastAPI  ─ middleware: request id · rate limit · size limit · request log ─ v1 routes (/predict, /explain …)
           |                 |                         |
     /api/v2 router     /fhir/r4 mock server     exception → {"error": {code, message, request_id}}
           |
   AccessContext (X-Demo-Persona)  ──>  PolicyEngine (default deny)  ──>  AuditLog
           |
   AgentService ── LangGraph: guard → plan → act* → compose → verify → review_gate → finalize
           |                     |          |           |          |
           |              planner (rules |  tools     composer   verifier (citations, numbers,
           |              or LLM)        (typed,      (template   computations, lexical support,
           |                              scoped)      or LLM)     unsafe-output rules)
           v
   ClinicalRecords  ·  RetrievalIndex (per-patient BM25 + LSA + RRF)  ·  risk_bridge → src.predict (XGBoost + SHAP)
           |
   FHIRStore (versioned, lineage) ── SQLite (WAL) with forward-only migrations
           ^
   synthetic FHIR R4 fixture  ·  FHIRHttpClient (external read-only FHIR → validation → ingest)
```

## Components

| Layer | Module | Responsibility |
| --- | --- | --- |
| UI | `frontend/workspace/` | Overview, patients, agent, risk, evidence, evaluation, operations, governance. Typed client in `api.ts`; no business logic. |
| UI (v1) | `dashboard/`, `frontend/src/` | Original Streamlit dashboard and SHAP explorer component — kept working. |
| API | `api/v2.py`, `api/fhir.py`, `api/middleware.py`, `api/main.py` | Contracts, persona resolution, error mapping, telemetry. |
| Identity & policy | `src/safety/policy.py` | Personas, role permissions, patient panels, default deny, audit hook. |
| Orchestration | `src/agent/graph.py`, `service.py` | LangGraph state machine, budgets, cancellation, sessions, persistence of runs. |
| Tools | `src/agent/tools.py` | 11 typed, authorised, patient-bound tools with timeouts and structured errors. |
| Planning / generation | `src/agent/planner.py`, `composer.py`, `llm.py` | Rule or LLM tool selection; template or LLM answer drafting; OpenAI-compatible provider with retries and circuit breaker. |
| Safety | `src/safety/guards.py`, `verifier.py` | Input classification, injection flagging, output rules, claim verification. |
| Clinical data | `src/clinical/` | Synthetic generator, FHIR validation, versioned store, patient views, consistency checks, model-compatibility bridge, FHIR HTTP client. |
| Retrieval | `src/retrieval/` | Section chunking, BM25, LSA embeddings, RRF fusion, cache, metrics. |
| Governance | `src/governance/` | Artifact-versioned human review, audit log. |
| Persistence | `src/persistence/db.py` | SQLite connection, migrations (6 application versions). |
| ML (v1) | `src/train.py`, `predict.py`, `explainability.py` … | Unchanged readmission pipeline. |
| Evaluation | `evaluation/` | Datasets, runner, thresholds, results. |

## Key design decisions

- **Patient binding outside the model.** Tools never take a patient id argument; the id comes from the authorised session. A model cannot redirect a tool to another record. ([ADR 0002](ADR/0002-agent-safety-boundaries.md))
- **Verification as a gate, not a prompt.** Every returned claim is checked against evidence collected in the same run. Unverifiable claims are removed; if none survive, the agent abstains.
- **Structural retrieval isolation.** Each patient has a separate index partition; a post-condition check fails closed if a result ever crosses patients.
- **Two inference modes, always labelled.** Deterministic mode (rules + templates) is the default and the CI baseline. LLM mode uses any OpenAI-compatible endpoint; responses always state which mode produced them. ([ADR 0004](ADR/0004-inference-modes.md))
- **Dataset separation.** The UCI model is applied to synthetic FHIR patients only through `risk_bridge.py`, which refuses ineligible records and lists every approximation. ([ADR 0005](ADR/0005-model-applicability.md))
- **SQLite first.** Portable SQL, WAL mode, migrations; PostgreSQL/pgvector deferred. ([ADR 0003](ADR/0003-persistence.md))

See [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) for request flows and data models, and [SAFETY_AND_PRIVACY.md](SAFETY_AND_PRIVACY.md) for controls.

## Tradeoffs

| Choice | Benefit | Cost |
| --- | --- | --- |
| Modular monolith | One container, simple deploy, no network hops | Components scale together |
| LSA embeddings | Free, deterministic, tiny, no model download | Weaker semantics than neural encoders |
| Rule-based deterministic composer | Fast, reproducible, verifiable | Narrow coverage; abstains outside intents |
| Lexical-support verification | Cheap, explainable, catches off-topic text | Not semantic entailment; can reject valid paraphrases |
| Header-selected demo personas | No login for a public synthetic demo | Not authentication |
| Progress streaming | Real node/tool progress over SSE; answers only after verification | Unverified draft tokens are never streamed |

Durable checkpoints, explicit compatible resume, idempotency, cancellation ownership, retention and opt-in local OpenTelemetry are detailed in [RELIABILITY.md](RELIABILITY.md). Application migrations now include execution controls and persistent retrieval/evaluation metadata.
