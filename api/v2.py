"""``/api/v2`` - clinical workspace API (patients, agent, retrieval, review, ops).

Every route resolves an ``AccessContext`` from the ``X-Demo-Persona`` header
and authorises through the central policy. Domain exceptions are mapped to a
consistent ``{"error": {"code", "message", "request_id"}}`` body in
``api.main``.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from api.v2_schemas import (
    AgentRunRequest,
    AgentRunResponse,
    ReviewDecisionRequest,
    ReviewRevisionRequest,
    ReviewSubmitRequest,
    RetrievalRequest,
    RetrievalResponse,
    RunStatusResponse,
    RunFeedbackRequest,
)
from src import config
from src import observability
from src.agent.tools import tool_catalog
from src.clinical.risk_bridge import assess
from src.clinical.model_audit import calibration_summary
from src.persistence.db import utc_now
from src.errors import NotFoundError
from src.runtime import Runtime, get_runtime
from src.safety.policy import ALL_SYNTHETIC_PATIENTS, PERSONAS, PATIENT_SCOPED_ACTIONS, AccessContext, Action,     evaluate, resolve_persona

router = APIRouter(prefix="/api/v2", tags=["clinical workspace v2"])
EVALUATION_RESULTS = config.PROJECT_ROOT / "evaluation" / "results" / "latest.json"
EVALUATION_HISTORY = config.PROJECT_ROOT / "evaluation" / "results" / "history.jsonl"


class MissingPersona(PermissionError):
    pass


class WorkspaceRequired(PermissionError):
    pass


WORKSPACE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")


def public_mode() -> bool:
    """Public demo deployments isolate every visitor in an opaque per-browser workspace."""
    return os.getenv("CLINICAL_TRACE_PUBLIC_MODE", "").strip() == "1"


def runtime() -> Runtime:
    return get_runtime()


def access(request: Request, x_demo_persona: str | None = Header(None),
           x_demo_workspace: str | None = Header(None)) -> AccessContext:
    persona = resolve_persona(x_demo_persona)
    if persona is None:
        raise MissingPersona("select a synthetic demo persona with the X-Demo-Persona header")
    if x_demo_workspace is not None and not WORKSPACE_PATTERN.fullmatch(x_demo_workspace):
        raise WorkspaceRequired("the X-Demo-Workspace header is malformed")
    if x_demo_workspace is None and public_mode():
        raise WorkspaceRequired("public demo requests need an X-Demo-Workspace header")
    return AccessContext(persona=persona, request_id=getattr(request.state, "request_id", None),
                         workspace=x_demo_workspace)


# ------------------------------------------------------------------- system
@router.get("/system/status")
def system_status(rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    """Public readiness summary (no patient data)."""
    provider = rt.provider.describe() if rt.provider else None
    return {
        "service": "Clinical-Trace AI",
        "version": "2.0.0-dev",
        "status": "ready",
        "uptime_s": round(time.time() - rt.started_at, 1),
        "database": {"engine": "sqlite", "schema_version": rt.db.schema_version()},
        "data": {"patients": len(rt.store.patient_ids()), "startup_ingestion": rt.startup_ingestion,
                 "origin": "synthetic FHIR R4 fixture (no real patients)"},
        "retrieval": rt.index.stats(),
        "readmission_model": {"loaded": rt.model_available,
                              "metadata": _model_summary() if rt.model_available else None},
        "inference": {
            "mode": "llm" if rt.provider else "deterministic",
            "provider": provider,
            "label": ("Live language-model inference via the configured provider" if rt.provider else
                      "Deterministic mode: rule-based planning and template/extractive answers; no language "
                      "model is called."),
        },
        "evaluation": _latest_evaluation_summary(),
        "disclaimer": config.POSITIONING_STATEMENT,
    }


def _model_summary() -> dict[str, Any]:
    metadata = config.load_model_metadata()
    metrics = metadata.get("metrics", {})
    return {
        "model_type": metadata.get("model_type"),
        "model_version": metadata.get("model_version"),
        "training_date": metadata.get("training_date"),
        "risk_threshold": metadata.get("risk_threshold"),
        "test_metrics": {k: metrics.get(k) for k in ("roc_auc", "pr_auc", "recall_positive", "precision_positive",
                                                     "specificity", "f1_positive", "f2_positive")},
        "evaluation_protocol": metadata.get("evaluation_protocol"),
        "calibration": calibration_summary(config.PROJECT_ROOT / "evaluation/results/readmission_report.json",
                                             config.MODEL_PATH, config.MODEL_METADATA_PATH),
    }


@router.get("/personas")
def personas() -> list[dict[str, Any]]:
    return [
        {"persona_id": p.persona_id, "display_name": p.display_name, "role": p.role, "panel": list(p.panel),
         "description": p.description}
        for p in PERSONAS.values()
    ]


@router.get("/policy/matrix")
def policy_matrix() -> dict[str, Any]:
    """Evaluate the live policy for every persona x action (patient actions against each patient)."""
    rows = []
    for persona in PERSONAS.values():
        context = AccessContext(persona)
        for action in Action:
            if action in PATIENT_SCOPED_ACTIONS:
                cells = {pid: evaluate(context, action, pid).allowed for pid in ALL_SYNTHETIC_PATIENTS}
            else:
                cells = {"-": evaluate(context, action).allowed}
            rows.append({"persona": persona.persona_id, "role": persona.role, "action": action.value,
                         "patient_scoped": action in PATIENT_SCOPED_ACTIONS, "decisions": cells})
    return {"patients": list(ALL_SYNTHETIC_PATIENTS), "rows": rows,
            "note": "Computed from src/safety/policy.py at request time (default deny)."}


@router.get("/tools")
def tools() -> list[dict[str, Any]]:
    return tool_catalog()


# ----------------------------------------------------------------- patients
@router.get("/patients")
def list_patients(ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)) -> list[dict[str, Any]]:
    rt.policy.require(ctx, Action.PATIENT_LIST)
    items = []
    for pid in rt.store.patient_ids():
        if pid not in ctx.persona.panel:
            continue
        profile = rt.records.profile(pid)
        if profile is None:
            continue
        conditions = rt.records.conditions(pid)
        items.append({
            "patient_id": pid,
            "display_name": profile["display_name"],
            "gender": profile["gender"],
            "age_at_last_encounter": profile["age_at_last_encounter"],
            "conditions": [c["display"] for c in conditions],
            "counts": profile["counts"],
            "freshness": profile["freshness"],
        })
    return items


def _patient_guard(rt: Runtime, ctx: AccessContext, patient_id: str, action: Action = Action.PATIENT_READ) -> None:
    rt.policy.require(ctx, action, patient_id)
    if rt.store.get("Patient", patient_id) is None:
        raise NotFoundError("patient not found")


@router.get("/patients/{patient_id}")
def patient_detail(patient_id: str, ctx: AccessContext = Depends(access),
                   rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    _patient_guard(rt, ctx, patient_id)
    return {
        "profile": rt.records.profile(patient_id),
        "encounters": rt.records.encounters(patient_id),
        "conditions": rt.records.conditions(patient_id),
        "medications": rt.records.medications(patient_id),
        "lab_trends": rt.records.lab_trends(patient_id),
        "vitals": rt.records.observations(patient_id, category="vital-signs"),
        "social_history": rt.records.observations(patient_id, category="social-history"),
        "documents": rt.records.documents(patient_id),
        "diagnostic_reports": rt.records.diagnostic_reports(patient_id),
        "missing_information": rt.records.missing_information(patient_id),
    }


@router.get("/patients/{patient_id}/timeline")
def patient_timeline(patient_id: str, ctx: AccessContext = Depends(access),
                     rt: Runtime = Depends(runtime)) -> list[dict[str, Any]]:
    _patient_guard(rt, ctx, patient_id)
    return [{k: v for k, v in event.items() if k != "detail"} for event in rt.records.timeline(patient_id)]


@router.get("/patients/{patient_id}/documents/{document_id}")
def patient_document(patient_id: str, document_id: str, ctx: AccessContext = Depends(access),
                     rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    _patient_guard(rt, ctx, patient_id)
    document = rt.records.document_text(patient_id, document_id)
    if document is None:
        raise NotFoundError("document not found for this patient")
    provenance = rt.store.provenance("DocumentReference", document_id)
    chunks = [c.as_dict() for c in rt.index.chunks_for(patient_id) if c.source_ref == f"DocumentReference/{document_id}"]
    return {**document, "provenance": provenance, "chunks": chunks}


@router.get("/patients/{patient_id}/resources/{resource_type}/{resource_id}/provenance")
def resource_provenance(patient_id: str, resource_type: str, resource_id: str, ctx: AccessContext = Depends(access),
                        rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    _patient_guard(rt, ctx, patient_id)
    resource = rt.store.get(resource_type, resource_id)
    if resource is None or (resource_type != "Patient" and
                            (resource.get("subject") or {}).get("reference") != f"Patient/{patient_id}"):
        raise NotFoundError("resource not found for this patient")
    return {"provenance": rt.store.provenance(resource_type, resource_id),
            "history": [{"version": r["meta"]["versionId"], "last_updated": r["meta"]["lastUpdated"]}
                        for r in rt.store.history(resource_type, resource_id)]}


@router.get("/patients/{patient_id}/risk")
def patient_risk(patient_id: str, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    _patient_guard(rt, ctx, patient_id, Action.RISK_READ)
    assessment = assess(rt.records, patient_id)
    if not assessment["available"]:
        return {"available": False, "reasons": assessment["reasons"], "patient_id": patient_id}
    if not rt.model_available or rt.agent.predictor is None:
        return {"available": False, "reasons": ["Readmission model artifacts are not loaded on this server."],
                "patient_id": patient_id}
    prediction = rt.agent.predictor(assessment["features"])
    return {"available": True, "patient_id": patient_id, "encounter_id": assessment["encounter_id"],
            "prediction": prediction, "features": assessment["features"], "caveats": assessment["caveats"],
            "model": _model_summary(), "disclaimer": config.SAFETY_DISCLAIMER}


@router.post("/patients/{patient_id}/retrieve", response_model=RetrievalResponse)
def patient_retrieve(patient_id: str, body: RetrievalRequest, ctx: AccessContext = Depends(access),
                     rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    _patient_guard(rt, ctx, patient_id, Action.RETRIEVAL_SEARCH)
    return rt.index.search(patient_id, body.query, k=body.k, mode=body.mode, date_from=body.date_from,
                           date_to=body.date_to)


# -------------------------------------------------------------------- agent
@router.post("/agent/runs", response_model=AgentRunResponse)
def agent_run(body: AgentRunRequest, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime),
              idempotency_key: str | None = Header(None, min_length=1, max_length=128)
              ) -> dict[str, Any]:
    return rt.agent.run(ctx, body.patient_id, body.question, session_id=body.session_id,
                        idempotency_key=idempotency_key)


@router.post("/agent/runs/stream", response_class=StreamingResponse)
async def agent_run_stream(request: Request, body: AgentRunRequest, ctx: AccessContext = Depends(access),
                           rt: Runtime = Depends(runtime),
                           idempotency_key: str | None = Header(None, min_length=1, max_length=128)) -> Any:
    from api.agent_stream import stream_run

    return await stream_run(request, body, ctx, rt, idempotency_key)


@router.get("/agent/runs/{run_id}/status", response_model=RunStatusResponse)
def agent_run_status(run_id: str, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)
                     ) -> dict[str, Any]:
    return rt.agent.run_status(ctx, run_id)


@router.post("/agent/runs/{run_id}/resume", response_model=AgentRunResponse)
def agent_run_resume(run_id: str, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)
                     ) -> dict[str, Any]:
    return rt.agent.resume(ctx, run_id)


@router.get("/agent/runs/{run_id}", response_model=AgentRunResponse)
def agent_run_detail(run_id: str, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)
                     ) -> dict[str, Any]:
    result = rt.agent.get_run(ctx, run_id)
    if result is None:
        raise NotFoundError("run not found")
    return result


@router.post("/agent/runs/{run_id}/cancel")
def agent_run_cancel(run_id: str, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)
                     ) -> dict[str, Any]:
    return {"run_id": run_id, "cancel_requested": rt.agent.cancel(ctx, run_id)}


@router.get("/agent/sessions/{session_id}")
def agent_session(session_id: str, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)
                  ) -> dict[str, Any]:
    return rt.agent.session_history(ctx, session_id)


@router.get("/agent/recent")
def agent_recent(ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime),
                 limit: int = Query(10, ge=1, le=50)) -> list[dict[str, Any]]:
    rows = rt.db.all("SELECT run_id, session_id, patient_id, question, outcome, inference_mode, latency_ms, "
                     "created_at FROM agent_run WHERE persona=? ORDER BY created_at DESC LIMIT ?",
                     (ctx.principal, limit))
    return [dict(r) for r in rows if r["patient_id"] in ctx.persona.panel]


@router.post("/agent/runs/{run_id}/feedback")
def agent_feedback(run_id: str, body: RunFeedbackRequest, ctx: AccessContext = Depends(access),
                   rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    run = rt.agent.get_run(ctx, run_id)
    if run is None:
        raise NotFoundError("run not found")
    rt.policy.require(ctx, Action.FEEDBACK_SUBMIT, run["patient_id"])
    feedback_id = str(uuid.uuid4())
    rt.db.execute("INSERT INTO feedback(feedback_id, run_id, persona, category, rating, comment, created_at) "
                  "VALUES (?,?,?,?,?,?,?)", (feedback_id, run_id, ctx.principal, body.category,
                                             body.rating, body.comment, utc_now()))
    return {"feedback_id": feedback_id, "status": "recorded",
            "note": "Feedback is stored for offline review. It never changes model or agent behaviour automatically."}


# ------------------------------------------------------------------ reviews
@router.post("/reviews")
def review_submit(body: ReviewSubmitRequest, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)
                  ) -> dict[str, Any]:
    run = rt.agent.get_run(ctx, body.run_id)
    if run is None:
        raise NotFoundError("run not found")
    return rt.reviews.submit_run(ctx, run)


@router.get("/reviews")
def review_list(ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime),
                status: str | None = Query(None, max_length=20)) -> list[dict[str, Any]]:
    return rt.reviews.list(ctx, status)


@router.get("/reviews/{review_id}")
def review_detail(review_id: str, ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)
                  ) -> dict[str, Any]:
    return rt.reviews.get(ctx, review_id)


@router.post("/reviews/{review_id}/decision")
def review_decide(review_id: str, body: ReviewDecisionRequest, ctx: AccessContext = Depends(access),
                  rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    return rt.reviews.decide(ctx, review_id, body.decision, body.artifact_hash, body.comment)


@router.post("/reviews/{review_id}/revise")
def review_revise(review_id: str, body: ReviewRevisionRequest, ctx: AccessContext = Depends(access),
                  rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    return rt.reviews.revise(ctx, review_id, body.remove_claims, body.note)


# --------------------------------------------------------- governance / ops
@router.get("/audit")
def audit_events(ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime),
                 decision: str | None = Query(None, pattern="^(allow|deny)$"),
                 limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    rt.policy.require(ctx, Action.AUDIT_READ)
    scope = ctx if ctx.workspace else None
    return {"counts": rt.audit.counts(scope), "events": rt.audit.recent(limit, decision, scope)}


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1], 2)


@router.get("/ops/metrics")
def ops_metrics(ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    rt.policy.require(ctx, Action.OPS_READ)
    requests = rt.db.all("SELECT route, status_code, latency_ms FROM request_log ORDER BY ts DESC LIMIT 5000")
    runs = rt.db.all("SELECT outcome, inference_mode, latency_ms, trace FROM agent_run WHERE status != 'running' "
                     "ORDER BY created_at DESC LIMIT 1000")
    by_route: dict[str, list[Any]] = {}
    for row in requests:
        by_route.setdefault(row["route"], []).append(row)
    routes = [
        {"route": route, "count": len(rows),
         "error_rate": round(sum(1 for r in rows if r["status_code"] >= 500) / len(rows), 4),
         "client_error_rate": round(sum(1 for r in rows if 400 <= r["status_code"] < 500) / len(rows), 4),
         "p50_ms": _percentile([r["latency_ms"] for r in rows], 50),
         "p95_ms": _percentile([r["latency_ms"] for r in rows], 95)}
        for route, rows in sorted(by_route.items(), key=lambda kv: -len(kv[1]))
    ]
    tool_latency: dict[str, list[float]] = {}
    tool_errors: dict[str, int] = {}
    node_latency: dict[str, list[float]] = {}
    for run in runs:
        for span in json.loads(run["trace"] or "[]"):
            node_latency.setdefault(span["node"], []).append(span.get("duration_ms", 0.0))
            if span.get("tool"):
                tool_latency.setdefault(span["tool"], []).append(span.get("latency_ms", 0.0))
                if not span.get("ok"):
                    tool_errors[span["tool"]] = tool_errors.get(span["tool"], 0) + 1
    outcomes: dict[str, int] = {}
    for run in runs:
        outcomes[run["outcome"] or "unknown"] = outcomes.get(run["outcome"] or "unknown", 0) + 1
    run_latency = [r["latency_ms"] for r in runs if r["latency_ms"] is not None]
    from api.middleware import RATE_LIMITER  # local import avoids a cycle at module load
    return {
        "generated_at": utc_now(),
        "window": {"requests": len(requests), "agent_runs": len(runs)},
        "routes": routes,
        "agent": {
            "outcomes": outcomes,
            "p50_ms": _percentile(run_latency, 50),
            "p95_ms": _percentile(run_latency, 95),
            "modes": {m: sum(1 for r in runs if r["inference_mode"] == m) for m in {r["inference_mode"] for r in runs}},
        },
        "tools": [
            {"tool": tool, "calls": len(values), "errors": tool_errors.get(tool, 0),
             "p50_ms": _percentile(values, 50), "p95_ms": _percentile(values, 95)}
            for tool, values in sorted(tool_latency.items())
        ],
        "nodes": [{"node": node, "p50_ms": _percentile(v, 50), "p95_ms": _percentile(v, 95)}
                  for node, v in sorted(node_latency.items())],
        "retrieval": rt.index.stats(),
        "provider": rt.provider.describe() if rt.provider else {"provider": "none", "mode": "deterministic"},
        "execution": rt.agent.describe(),
        "telemetry": observability.describe(),
        "rate_limit": RATE_LIMITER.describe(),
        "note": "Metrics are computed from requests served by this process since its database was created; "
                "no values are simulated.",
    }


def _latest_evaluation_summary() -> dict[str, Any] | None:
    if not EVALUATION_RESULTS.exists():
        return None
    data = json.loads(EVALUATION_RESULTS.read_text(encoding="utf-8"))
    return {"run_at": data.get("run_at"), "dataset_version": data.get("dataset_version"),
            "gate_passed": data.get("gate", {}).get("passed"), "mode": data.get("mode")}


@router.get("/evaluation/latest")
def evaluation_latest(ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)) -> dict[str, Any]:
    rt.policy.require(ctx, Action.EVALUATION_READ)
    if not EVALUATION_RESULTS.exists():
        raise NotFoundError("no evaluation results have been generated; run python -m evaluation.run_eval")
    data = json.loads(EVALUATION_RESULTS.read_text(encoding="utf-8"))
    history: list[dict[str, Any]] = []
    if EVALUATION_HISTORY.exists():
        for line in Path(EVALUATION_HISTORY).read_text(encoding="utf-8").splitlines()[-30:]:
            if line.strip():
                history.append(json.loads(line))
    comparisons = []
    for path in sorted(EVALUATION_RESULTS.parent.glob("llm_*.json")):
        other = json.loads(path.read_text(encoding="utf-8"))
        comparisons.append({"file": path.name, "run_at": other.get("run_at"), "mode": other.get("mode"),
                            "provider": other.get("provider"), "gate": other.get("gate"),
                            "summary": other.get("summary"),
                            "failed_cases": [c for c in other.get("cases", []) if not c.get("passed")]})
    return {**data, "history": history, "comparisons": comparisons}
