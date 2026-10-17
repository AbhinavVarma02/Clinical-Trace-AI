# API Reference

Interactive OpenAPI docs are served at `/docs`. All v2 and FHIR routes except those marked *public* require the `X-Demo-Persona` header (`clinician.demo`, `clinician.restricted`, `reviewer.demo`, `admin.demo`).

Errors from v2 use one shape:

```json
{"error": {"code": "forbidden", "message": "patient 'synthetic_p002' is outside the persona's panel", "request_id": "…"}}
```

| Status | Code | When |
| --- | --- | --- |
| 401 | `persona_required` | Missing or unknown persona |
| 403 | `forbidden` / `session_mismatch` | Policy denial; session belongs to another persona or patient |
| 404 | `not_found` | Unknown resource, or a resource of another patient |
| 409 | `conflict` | Review decision on a stale artifact hash |
| 413 | `payload_too_large` | Body over 32 KB |
| 422 | (FastAPI) | Schema validation failure |
| 429 | `rate_limited` | Token bucket exhausted (`Retry-After` set) |

## v2 — system

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v2/system/status` | *Public.* Readiness, data counts, index stats, model metadata, inference mode, latest evaluation gate |
| GET | `/api/v2/personas` | *Public.* Demo personas and panels |
| GET | `/api/v2/policy/matrix` | *Public.* Live policy decisions for every persona × action |
| GET | `/api/v2/tools` | *Public.* Agent tool catalogue with JSON schemas |

## v2 — patients and retrieval (clinician, reviewer)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v2/patients` | Patients in the persona's panel |
| GET | `/api/v2/patients/{id}` | Profile, encounters, conditions, medications (with versions), lab trends, vitals, documents, documentation gaps |
| GET | `/api/v2/patients/{id}/timeline` | Merged chronological events with source references |
| GET | `/api/v2/patients/{id}/documents/{doc}` | Decoded note text, provenance, retrieval chunks |
| GET | `/api/v2/patients/{id}/resources/{type}/{rid}/provenance` | Provenance and version history |
| GET | `/api/v2/patients/{id}/risk` | Readmission prediction where applicable, else `available: false` with reasons |
| POST | `/api/v2/patients/{id}/retrieve` | `{query, k≤20, mode: hybrid|bm25|dense, date_from?, date_to?}` → ranked chunks with scores and diagnostics |

## v2 — agent (clinician)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v2/agent/runs` | `{patient_id, question≤1000, session_id?}` → run result |
| GET | `/api/v2/agent/runs/{run_id}` | Owner, or a reviewer for a patient in their panel |
| POST | `/api/v2/agent/runs/{run_id}/cancel` | Requests cancellation of an in-flight run |
| GET | `/api/v2/agent/sessions/{session_id}` | Questions and outcomes in a session |
| GET | `/api/v2/agent/recent` | Recent runs for the persona |
| POST | `/api/v2/agent/runs/{run_id}/feedback` | `{category, rating?, comment?}`; stored only |

Run result (abridged):

```json
{
  "run_id": "…", "session_id": "…", "patient_id": "synthetic_p001", "question": "Why was metformin stopped?",
  "outcome": "answered",
  "answer": {"summary": "…", "claims": [{"text": "…", "citations": ["p001-m1@v2"], "kind": "medication_status"}],
             "rejected_claims": [], "uncertainty": [], "abstain_reason": null},
  "refusal": null,
  "risk": null,
  "evidence": [{"evidence_id": "p001-m1@v2", "source_ref": "MedicationRequest/p001-m1", "source_version": "2",
                "text": "…", "kind": "medication", "cited": true, "flags": [], "scores": null}],
  "plan": {"source": "deterministic-rules", "intents": ["medications"], "calls": […]},
  "tool_calls": [{"tool": "get_medication_history", "ok": true, "evidence_count": 5, "latency_ms": 3.1}],
  "verification": {"status": "verified", "claims_total": 6, "claims_supported": 6, "isolation_violations": 0},
  "review": {"required": false, "reasons": []},
  "inference": {"mode": "deterministic", "planner": "deterministic-rules", "composer": "deterministic-template",
                "fallback_used": false, "llm_attempt": null, "label": "Deterministic mode: … no language model was called."},
  "trace": [{"node": "guard", "duration_ms": 0.4, "verdict": "allow"}, …],
  "latency_ms": 12.7,
  "disclaimer": "This is decision-support only and not medical advice."
}
```

`outcome` is one of `answered`, `abstained` (no claim survived verification or no record information), `refused` (guard category and message in `refusal`), `cancelled`, `error`.

## v2 — review and governance

| Method | Path | Role |
| --- | --- | --- |
| POST | `/api/v2/reviews` | clinician: `{run_id}` → review on artifact v1 |
| GET | `/api/v2/reviews` | clinician / reviewer / admin (admin sees metadata only) |
| GET | `/api/v2/reviews/{id}` | Review, artifact (if patient readable), events, `approval_valid` |
| POST | `/api/v2/reviews/{id}/decision` | reviewer: `{decision: approved|rejected|needs_revision, artifact_hash, comment?}` |
| POST | `/api/v2/reviews/{id}/revise` | submitter: `{remove_claims: [index], note?}` → new artifact version, supersedes prior reviews |
| GET | `/api/v2/audit?decision=deny` | admin |
| GET | `/api/v2/ops/metrics` | admin: route, agent, tool, and node latency; provider and rate-limit status |
| GET | `/api/v2/evaluation/latest` | latest deterministic report, LLM comparisons, history |

## Mock FHIR R4 (read-only, synthetic)

| Method | Path |
| --- | --- |
| GET | `/fhir/r4/metadata` (*public* CapabilityStatement) |
| GET | `/fhir/r4/{type}/{id}` |
| GET | `/fhir/r4/{type}/{id}/_history/{version}` |
| GET | `/fhir/r4/{type}?patient={id}&_count=&_offset=` (searchset Bundle with `next`/`previous` links) |

Supported types: Patient, Encounter, Condition, Observation, MedicationRequest, DocumentReference, DiagnosticReport. Resources outside the persona's panel return 404 on read (existence is not revealed) and 403 on search.

## v1 (unchanged)

`GET /`, `GET /health`, `GET /model-info`, `POST /predict`, `POST /explain`, `POST /feedback` — see the README.

## Durable execution controls

`POST /api/v2/agent/runs` accepts an optional `Idempotency-Key` header (1–128 characters). `GET /api/v2/agent/runs/{id}/status`, `POST /api/v2/agent/runs/{id}/resume` and cancellation are owner-only. Resume returns 409 for incompatible/completed/uncheckpointed runs. Admission exhaustion returns 503 with Retry-After. See [RELIABILITY.md](RELIABILITY.md) for recovery constraints.

`POST /api/v2/agent/runs/stream` accepts the same body/persona/idempotency header and returns `text/event-stream`. Events: `progress` (run_id/node/duration, optional tool/ok/error_type), then `result` (AgentRunResponse) or `error`. Heartbeat comments carry no record content. Only the final response contains the answer. No token-level clinical drafts are exposed.
