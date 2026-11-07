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

## Beta M2.3 target: owner-only live sandbox refresh

Approved product requirement, 2026-11-06 (America/New_York); **not implemented or deployed**. The Stable architecture above remains the deployed reference. [ADR 0009](ADR/0009-beta-channel-and-data-sources.md) records the Beta amendment.

**Beta UI → Approved synthetic patient selection → Owner-authorized Epic refresh → Secure backend OAuth → Live Epic FHIR sandbox API → Atomic validated import → Existing Clinical-Trace evidence engine → Verified answers, citations and human review.**

The backend must authenticate the owner, map a selection to an owner-approved sandbox-patient allowlist and authorise the refresh before any outbound call. Browser inputs cannot select arbitrary Epic hosts, paths, patients, scopes or keys. M1's OAuth authenticates the application to Epic; it does not authenticate a UI user. Demo personas, public workspace identifiers and the Stable edge secret are insufficient for owner identity. The owner authentication/session design remains a prerequisite decision, including expiry, revocation and CSRF protection if cookies are used.

### Existing capabilities verified against code

| Capability | Reusable implementation | Current boundary |
|---|---|---|
| Sandbox OAuth | `src/interop/epic/auth.py`, `keys.py`, `settings.py`: RS384 `private_key_jwt`, short-lived assertions, locked in-memory token cache, refresh on expiry; encrypted local key/JWKS tooling | CLI configuration and owner passphrase flow; hosted secret custody and owner login are missing |
| Read-only Epic client | `src/interop/epic/client.py`: pinned sandbox URLs, no redirects/environment proxies, patient checks, bounded pagination/body sizes/timeouts/retries, one token refresh after 401 | No authenticated refresh API, approved-patient registry or per-owner refresh budget |
| Fetch and map | `importer.py`, `mapping.py`: bounded note selection/Binary reads, operation outcomes, existing `validate_resource` gate, counted mapping rejections | Some failed operations can produce a partial fetch; MedicationRequest remains disabled pending A1 |
| Immutable snapshots | `workspace.py`: unique snapshot IDs, separate storage, bundle hash, mapping/manifest versions, isolated runtime, patient checks | Writes occur directly in a new directory with multiple manifest writes; no atomic promotion, active-version pointer or rollback coordinator |
| Source isolation/provenance | `src/sources.py`, `api/v2.py`: per-source runtime, source status, coverage and field lineage, retrieval-time caveat, deterministic/tracing guards | Registry is configured at startup; no hot swap. Duplicate patient IDs across sources are refused; medication coverage collapses skipped/failed/unrecorded searches |
| Evidence and review | `src/agent/`, `src/clinical/`, `src/retrieval/`, `src/safety/`, `src/governance/`: scoped tools, verifier, citations and human review | Refresh must preserve snapshot binding through sessions, runs, caches and reviews; Epic-source application state is currently in memory |
| Operational controls | `api/middleware.py`, `src/interop/epic/telemetry.py`: generic API rate/size limits, content-free Epic counters/latencies and categorical errors | Generic limits are not a complete authenticated refresh budget or durable refresh audit |

### Required refresh and promotion boundary

1. Create a bounded refresh operation bound to the authenticated owner, approved patient, requested resource scope and current snapshot generation. Apply per-owner/per-patient and global budgets, a concurrent-refresh limit, deadline and duplicate-request control. Acquire tokens only on the backend; never pass a token, assertion, key or passphrase to the browser, logs or build artifacts.
2. Fetch into a staging location that cannot be served. Record request start/end, outcome, source/mode, requested categories and mapping/import counts. Track the last attempted refresh separately from the last successfully promoted retrieval. A successful API response can contain unchanged sandbox data.
3. Validate patient/source isolation, resource contracts, integrity, query completion, counted exclusions and the owner-approved completeness policy; construct and verify the new evidence runtime before promotion. Failed required queries block promotion by default. Any policy allowing incomplete imports needs explicit approval and visible coverage. Unresolved references remain explicit availability limitations; they never trigger guessed upstream lookups.
4. Atomically publish the validated snapshot and its runtime generation, with crash recovery and a check that an older concurrent attempt cannot replace a newer result. This requires new coordination around M1, not an assumption that directory creation is atomic publication. Each answer uses one pinned snapshot throughout. Snapshot generations are distinct from Epic source versions and the existing per-snapshot internal resource version `1`.
5. Preserve prior snapshots, provenance, runs and review bindings for an explicit, audited rollback. In-flight answers/reviews must retain their original generation. Do not register successive snapshots of the same patient as ordinary parallel sources: M2-S1 rejects that configuration. Generation handling, retention and storage durability need design before implementation.
6. On refresh failure, expose the categorical error and last attempt time. An owner may explicitly continue with an available snapshot labelled cached, with its original retrieval time/version. No silent fallback, fake timestamp update or automatic switch to synthetic data. An agent answer reads the selected snapshot and never calls Epic directly.

Coverage must distinguish query execution from import outcome: **not requested** (outside this request), **not imported** (excluded/unsupported/rejected), **returned none** (successful completed query returned zero), **failed** (request did not complete successfully), and **unresolved reference** (an imported resource points to unavailable evidence). Mapping rejects, note caps and partially returned results require counts/reasons alongside these states. Failure must never be relabelled returned none; zero accepted resources alone does not establish returned none. Unknown legacy coverage remains unknown. Neither coverage nor retrieval metadata proves clinical absence.

Minimal refresh audit events should contain an opaque operation/owner reference, timestamps, action/outcome code, snapshot generation, scope category and counts. Exclude credentials, raw URLs/query strings, upstream identifiers, note text and FHIR payloads. Define retention and access separately from clinical evidence storage; existing content-free Epic telemetry is the starting point.

All Beta compute, edge routes/bindings, databases/volumes, secrets/signing-key custody, logs and release/rollback controls must be isolated from Stable. No Stable co-host reuse or inherited public route exposure is approved. Hosting and Cloudflare account placement remain owner decisions. Security/red-team verification uses local MOCK token/FHIR services and synthetic fixtures only; a separately approved bounded live smoke is functional verification, never an attack against Epic.

Acceptance, unresolved risks and required decisions are in [EPIC_SANDBOX.md](EPIC_SANDBOX.md#m23-mandatory-controlled-real-time-retrieval); sequencing is in [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md#beta-roadmap-mandatory-real-time-epic-sandbox-retrieval).

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
