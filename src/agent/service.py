"""Agent service: session management, graph execution, result shaping, persistence."""

from __future__ import annotations

import json
import hashlib
import os
import threading
from contextlib import contextmanager
import time
import uuid
from typing import Any, Callable, Iterator

from src.agent.checkpoints import execution_revision, open_checkpoints
from src.agent.graph import CANCELLATION, RECURSION_LIMIT, CancelledRun, build_graph
from src.agent.llm import OpenAICompatibleProvider
from src.agent.tools import ToolContext
from src.clinical.records import ClinicalRecords
from src.config import SAFETY_DISCLAIMER
from src.errors import NotFoundError
from src.persistence.db import Database, dumps, utc_now
from src.retrieval.index import RetrievalIndex
from src.safety.policy import AccessContext, Action, PolicyEngine, in_scope


def _inference_label(mode: str, degraded: str | None) -> str:
    if mode != "llm":
        return "Deterministic mode: rule-based planning and template/extractive answers; no language model was called."
    if degraded == "quota_exhausted":
        return ("Live inference paused: the daily free inference allocation is used up (resets 00:00 UTC). "
                "This run used the deterministic planner and composer.")
    if degraded == "provider_unavailable":
        return "Live inference was unavailable for this run; the deterministic planner and composer answered."
    return "Live language-model inference"

class SessionMismatch(PermissionError):
    pass


class AgentBusy(RuntimeError):
    pass


class RunConflict(RuntimeError):
    pass


class AgentService:
    def __init__(
        self,
        db: Database,
        records: ClinicalRecords,
        index: RetrievalIndex,
        policy: PolicyEngine,
        provider: OpenAICompatibleProvider | None = None,
        predictor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.db = db
        self.records = records
        self.index = index
        self.policy = policy
        self.provider = provider
        self.predictor = predictor
        self.checkpoints = open_checkpoints(db.path)
        self.revision = execution_revision(provider, predictor is not None)
        self.capacity = max(1, int(os.getenv("CLINICAL_TRACE_MAX_AGENT_RUNS", "4")))
        self._slots = threading.BoundedSemaphore(self.capacity)
        self._condition = threading.Condition()
        self._active = 0
        self._closing = False
        self._closed = False

    @contextmanager
    def _slot(self) -> Iterator[None]:
        with self._condition:
            if self._closing or not self._slots.acquire(blocking=False):
                raise AgentBusy("agent capacity unavailable; retry shortly")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._slots.release()
                self._condition.notify_all()

    def describe(self) -> dict[str, Any]:
        with self._condition:
            return {"capacity": self.capacity, "active": self._active, "closing": self._closing,
                    "checkpoints": "sqlite", "resume_limit": 3}

    def _data_version(self) -> str:
        digest = hashlib.sha256(self.index.version.encode())
        for row in self.db.all("SELECT resource_type, resource_id, version_id, content_hash FROM fhir_resource "
                               "WHERE is_current=1 ORDER BY resource_type, resource_id"):
            digest.update(dumps(list(row)).encode())
        return digest.hexdigest()

    def close(self, grace_s: float = 10.0) -> bool:
        """Stop admission, cooperatively cancel and drain. Never close a live DB."""
        with self._condition:
            self._closing = True
            drained = self._condition.wait_for(lambda: self._active == 0, timeout=grace_s)
            if drained and not self._closed:
                self.checkpoints.conn.close()
                self._closed = True
            return drained

    # -------------------------------------------------------------- sessions
    def _session(self, access: AccessContext, patient_id: str, session_id: str | None) -> str:
        persona = access.principal
        if session_id:
            row = self.db.one("SELECT persona, patient_id FROM agent_session WHERE session_id=?", (session_id,))
            if row is None:
                raise SessionMismatch("unknown session")
            if row["persona"] != persona or row["patient_id"] != patient_id:
                raise SessionMismatch("session belongs to a different persona or patient")
            self.db.execute("UPDATE agent_session SET updated_at=? WHERE session_id=?", (utc_now(), session_id))
            return session_id
        session_id = str(uuid.uuid4())
        now = utc_now()
        self.db.execute(
            "INSERT INTO agent_session(session_id, persona, patient_id, created_at, updated_at) VALUES (?,?,?,?,?)",
            (session_id, persona, patient_id, now, now),
        )
        return session_id

    def session_history(self, access: AccessContext, session_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM agent_session WHERE session_id=?", (session_id,))
        if row is None or row["persona"] != access.principal:
            raise SessionMismatch("unknown session")
        self.policy.require(access, Action.PATIENT_READ, row["patient_id"])
        runs = self.db.all("SELECT run_id, question, outcome, created_at FROM agent_run WHERE session_id=? "
                           "ORDER BY created_at", (session_id,))
        return {"session_id": session_id, "patient_id": row["patient_id"], "runs": [dict(r) for r in runs]}

    def get_run(self, access: AccessContext, run_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM agent_run WHERE run_id=?", (run_id,))
        if row is None:
            return None
        if not in_scope(access, row["persona"]):
            return None  # another visitor's workspace: indistinguishable from a missing run
        if row["persona"] != access.principal:
            # Runs are private to their persona; only reviewers may open other personas' runs.
            self.policy.require(access, Action.REVIEW_DECIDE, row["patient_id"])
        self.policy.require(access, Action.PATIENT_READ, row["patient_id"])
        return json.loads(row["result"]) if row["result"] else None

    def _owned_run(self, access: AccessContext, run_id: str) -> Any:
        row = self.db.one("SELECT * FROM agent_run WHERE run_id=?", (run_id,))
        if row is None:
            raise NotFoundError("run not found")
        if row["persona"] != access.principal:
            raise SessionMismatch("run belongs to a different persona")
        self.policy.require(access, Action.AGENT_RUN, row["patient_id"])
        return row

    def cancel(self, access: AccessContext, run_id: str) -> bool:
        self._owned_run(access, run_id)
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE agent_run SET cancel_requested=1 WHERE run_id=? AND status IN ('running','interrupted')",
                (run_id,),
            ).rowcount
        if changed:
            CANCELLATION.cancel(run_id)
        return bool(changed)

    def run_status(self, access: AccessContext, run_id: str) -> dict[str, Any]:
        row = self._owned_run(access, run_id)
        return {"run_id": run_id, "session_id": row["session_id"], "status": row["status"],
                "outcome": row["outcome"], "cancel_requested": bool(row["cancel_requested"]),
                "resume_count": row["resume_count"]}

    # ------------------------------------------------------------------- run
    def run(
        self,
        access: AccessContext,
        patient_id: str,
        question: str,
        session_id: str | None = None,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self.policy.require(access, Action.AGENT_RUN, patient_id)
        if self.records.profile(patient_id) is None:
            raise NotFoundError("patient not found")
        if idempotency_key is not None and not (1 <= len(idempotency_key) <= 128):
            raise RunConflict("idempotency key must contain 1 to 128 characters")
        with self._slot():
            with self.db.transaction() as conn:
                previous = conn.execute("SELECT * FROM agent_run WHERE persona=? AND idempotency_key=?",
                                        (access.principal, idempotency_key)).fetchone() if idempotency_key else None
                if previous:
                    if (previous["patient_id"] != patient_id or previous["question"] != question
                            or (session_id is not None and previous["session_id"] != session_id)):
                        raise RunConflict("idempotency key already used for a different request")
                    if previous["result"]:
                        return json.loads(previous["result"])
                    raise RunConflict("run already exists; inspect its status and explicitly resume if interrupted")
                session_id = self._session(access, patient_id, session_id)
                run_id = run_id or str(uuid.uuid4())
                mode = "llm" if self.provider else "deterministic"
                conn.execute(
                    "INSERT INTO agent_run(run_id, session_id, persona, patient_id, question, status, inference_mode, "
                    "created_at, idempotency_key, engine_revision, data_version) VALUES (?,?,?,?,?, 'running', ?, ?,?,?,?)",
                    (run_id, session_id, access.principal, patient_id, question, mode, utc_now(),
                     idempotency_key, self.revision, self._data_version()),
                )
            return self._execute(access, run_id, session_id, patient_id, question, mode, resume=False, observer=observer)

    def resume(self, access: AccessContext, run_id: str) -> dict[str, Any]:
        row = self._owned_run(access, run_id)
        with self._slot():
            with self.db.transaction() as conn:
                row = conn.execute("SELECT * FROM agent_run WHERE run_id=?", (run_id,)).fetchone()
                if row["status"] not in {"interrupted", "failed"} or row["resume_count"] >= 3:
                    raise RunConflict("only interrupted/failed runs may resume, at most three times")
                if row["engine_revision"] != self.revision or row["data_version"] != self._data_version():
                    raise RunConflict("code, inference configuration or data changed; start a new run")
                if self.checkpoints.get_tuple({"configurable": {"thread_id": run_id}}) is None:
                    raise RunConflict("no durable checkpoint exists; start a new run")
                conn.execute("UPDATE agent_run SET status='running', resume_count=resume_count+1 WHERE run_id=?", (run_id,))
            self._session(access, row["patient_id"], row["session_id"])
            return self._execute(access, run_id, row["session_id"], row["patient_id"], row["question"],
                                 row["inference_mode"], resume=True)

    def _execute(self, access: AccessContext, run_id: str, session_id: str, patient_id: str,
                 question: str, mode: str, resume: bool,
                 observer: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        ctx = ToolContext(access=access, policy=self.policy, records=self.records, index=self.index,
                          patient_id=patient_id, predictor=self.predictor)
        deadline = time.monotonic() + 180

        def cancelled() -> bool:
            row = self.db.one("SELECT cancel_requested FROM agent_run WHERE run_id=?", (run_id,))
            return self._closing or bool(row and row["cancel_requested"])

        def check_control() -> bool:
            if time.monotonic() > deadline:
                raise TimeoutError("agent run time budget exhausted")
            return cancelled()

        graph = build_graph(ctx, self.provider, checkpointer=self.checkpoints, check_cancel=check_control,
                            observer=observer)
        graph_config = {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}
        started = time.perf_counter()
        CANCELLATION.register(run_id)
        status = "completed"
        try:
            state = graph.invoke(
                None if resume else {"run_id": run_id, "question": question, "patient_id": patient_id,
                                     "other_patient_markers": self._other_patient_markers(patient_id), "trace": []},
                config=graph_config,
            )
            # Close the small race between the final node and a cancellation request.
            if cancelled():
                raise CancelledRun(run_id)
        except Exception as exc:  # noqa: BLE001 - safe failure, retained checkpoint
            try:
                trace = graph.get_state(graph_config).values.get("trace", [])
            except Exception:
                trace = []
            is_cancelled = isinstance(exc, CancelledRun)
            state = {"outcome": "cancelled" if is_cancelled else "error", "trace": trace,
                     "error": None if is_cancelled else type(exc).__name__}
            status = "cancelled" if is_cancelled else "failed"
        finally:
            CANCELLATION.release(run_id)
        latency_ms = (time.perf_counter() - started) * 1000
        result = self._shape(run_id, session_id, patient_id, question, state, latency_ms, mode)
        self.db.execute(
            "UPDATE agent_run SET status=?, outcome=?, result=?, trace=?, latency_ms=? WHERE run_id=?",
            (status, result["outcome"], dumps(result), dumps(result["trace"]), latency_ms, run_id),
        )
        return result

    def _other_patient_markers(self, patient_id: str) -> tuple[str, ...]:
        markers: list[str] = []
        for pid in self.records.store.patient_ids():
            if pid == patient_id:
                continue
            markers.append(pid)
            markers.append(pid.replace("synthetic_", ""))
            patient = self.records.store.get("Patient", pid) or {}
            for name in patient.get("name") or []:
                markers.extend(name.get("given") or [])
        own = self.records.store.get("Patient", patient_id) or {}
        own_names = {g for n in own.get("name") or [] for g in n.get("given") or []}
        return tuple(m for m in markers if m not in own_names)

    @staticmethod
    def _risk_block(results: dict[str, Any]) -> dict[str, Any] | None:
        result = results.get("get_model_explanations") or results.get("get_readmission_prediction")
        if result is None or not result.ok or not result.data:
            return None
        data = result.data
        if not data.get("available"):
            return {"available": False, "reasons": data.get("reasons", [])}
        prediction = data["prediction"]
        return {
            "available": True,
            "encounter_id": data["encounter_id"],
            "risk_probability": prediction["risk_probability"],
            "risk_label": prediction["readmission_risk"],
            "risk_threshold": prediction["risk_threshold"],
            "model_version": prediction["model_version"],
            "top_features": prediction["top_features"],
            "caveats": data["caveats"],
        }

    def _shape(self, run_id: str, session_id: str, patient_id: str, question: str, state: dict[str, Any],
               latency_ms: float, mode: str) -> dict[str, Any]:
        guard = state.get("guard")
        plan = state.get("plan")
        draft = state.get("draft")
        verification = state.get("verification") or {}
        evidence = state.get("evidence") or {}
        results = state.get("results") or {}
        accepted = verification.get("accepted", [])
        cited = {c for claim in accepted for c in claim["citations"]}
        outcome = state.get("outcome", "error")

        summary = None
        if draft is not None and draft.summary and verification.get("summary_verified"):
            summary = draft.summary
        elif outcome == "answered":
            sources = {evidence[c].source_ref for c in cited if c in evidence}
            summary = f"{len(accepted)} verified statement(s) drawn from {len(sources)} source record(s)."
        abstain_reason = None
        if outcome == "abstained":
            abstain_reason = (draft.abstain_reason if draft and draft.abstain_reason
                              else "No claim survived evidence verification, so no answer is given.")
        provider = self.provider.describe() if self.provider else None
        notes = " ".join((plan.notes if plan else []) + (draft.notes if draft else []))
        degraded = None
        if mode == "llm" and "ProviderQuotaExhausted" in notes:
            degraded = "quota_exhausted"
        elif mode == "llm" and "ProviderUnavailable" in notes:
            degraded = "provider_unavailable"
        return {
            "run_id": run_id,
            "session_id": session_id,
            "patient_id": patient_id,
            "question": question,
            "outcome": outcome,
            "answer": {
                "summary": summary,
                "claims": accepted,
                "rejected_claims": verification.get("rejected", []),
                "uncertainty": draft.uncertainty if draft else [],
                "abstain_reason": abstain_reason,
            },
            "refusal": ({"category": guard.category, "message": guard.message}
                        if guard is not None and not guard.allowed else None),
            "evidence": sorted(
                [{**item.as_dict(), "cited": item.evidence_id in cited} for item in evidence.values()],
                key=lambda e: (not e["cited"], e["kind"] != "retrieved", e["evidence_id"]),
            )[:80],
            "risk": self._risk_block(results),
            "plan": ({"source": plan.source, "intents": plan.intents, "notes": plan.notes,
                      "calls": plan.calls} if plan else None),
            "tool_calls": [{**r.summary(), "args": next((c["args"] for c in plan.calls if c["name"] == name), {})
                            if plan else {}} for name, r in results.items()],
            "verification": {k: verification.get(k) for k in
                             ("status", "claims_total", "claims_supported", "supported_ratio",
                              "isolation_violations", "summary_verified")} if verification else None,
            "review": state.get("review") or {"required": False, "reasons": []},
            "inference": {
                "mode": mode,
                "planner": plan.source if plan else None,
                "composer": draft.composer if draft else None,
                "fallback_used": bool(state.get("fallback_used")),
                "llm_attempt": state.get("llm_attempt"),
                "provider": provider,
                "degraded_reason": degraded,
                "label": _inference_label(mode, degraded),
            },
            "trace": state.get("trace", []),
            "error": state.get("error"),
            "latency_ms": round(latency_ms, 2),
            "created_at": utc_now(),
            "disclaimer": SAFETY_DISCLAIMER,
        }
