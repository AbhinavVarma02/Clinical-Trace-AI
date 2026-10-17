"""Agent service: session management, graph execution, result shaping, persistence."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from src.agent.graph import CANCELLATION, RECURSION_LIMIT, CancelledRun, build_graph
from src.agent.llm import OpenAICompatibleProvider
from src.agent.tools import ToolContext
from src.clinical.records import ClinicalRecords
from src.config import SAFETY_DISCLAIMER
from src.errors import NotFoundError
from src.persistence.db import Database, dumps, utc_now
from src.retrieval.index import RetrievalIndex
from src.safety.policy import AccessContext, Action, PolicyEngine


class SessionMismatch(PermissionError):
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

    # -------------------------------------------------------------- sessions
    def _session(self, access: AccessContext, patient_id: str, session_id: str | None) -> str:
        persona = access.persona.persona_id
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
        if row is None or row["persona"] != access.persona.persona_id:
            raise SessionMismatch("unknown session")
        self.policy.require(access, Action.PATIENT_READ, row["patient_id"])
        runs = self.db.all("SELECT run_id, question, outcome, created_at FROM agent_run WHERE session_id=? "
                           "ORDER BY created_at", (session_id,))
        return {"session_id": session_id, "patient_id": row["patient_id"], "runs": [dict(r) for r in runs]}

    def get_run(self, access: AccessContext, run_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM agent_run WHERE run_id=?", (run_id,))
        if row is None:
            return None
        if row["persona"] != access.persona.persona_id:
            # Runs are private to their persona; only reviewers may open other personas' runs.
            self.policy.require(access, Action.REVIEW_DECIDE, row["patient_id"])
        self.policy.require(access, Action.PATIENT_READ, row["patient_id"])
        return json.loads(row["result"]) if row["result"] else None

    def cancel(self, access: AccessContext, run_id: str) -> bool:
        self.policy.require(access, Action.AGENT_RUN, self._run_patient(run_id))
        return CANCELLATION.cancel(run_id)

    def _run_patient(self, run_id: str) -> str | None:
        row = self.db.one("SELECT patient_id FROM agent_run WHERE run_id=?", (run_id,))
        return row["patient_id"] if row else None

    # ------------------------------------------------------------------- run
    def run(
        self,
        access: AccessContext,
        patient_id: str,
        question: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self.policy.require(access, Action.AGENT_RUN, patient_id)
        if self.records.profile(patient_id) is None:
            raise NotFoundError("patient not found")
        session_id = self._session(access, patient_id, session_id)
        run_id = run_id or str(uuid.uuid4())
        mode = "llm" if self.provider else "deterministic"
        self.db.execute(
            "INSERT INTO agent_run(run_id, session_id, persona, patient_id, question, status, inference_mode, "
            "created_at) VALUES (?,?,?,?,?, 'running', ?, ?)",
            (run_id, session_id, access.persona.persona_id, patient_id, question[:1000], mode, utc_now()),
        )
        ctx = ToolContext(access=access, policy=self.policy, records=self.records, index=self.index,
                          patient_id=patient_id, predictor=self.predictor)
        graph = build_graph(ctx, self.provider)
        markers = self._other_patient_markers(patient_id)
        started = time.perf_counter()
        CANCELLATION.register(run_id)
        status = "completed"
        try:
            state = graph.invoke(
                {"run_id": run_id, "question": question, "patient_id": patient_id,
                 "other_patient_markers": markers, "trace": []},
                config={"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT},
            )
        except CancelledRun:
            state = {"outcome": "cancelled", "trace": []}
            status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - recorded as a failed run, never leaked
            state = {"outcome": "error", "trace": [], "error": type(exc).__name__}
            status = "failed"
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
                "label": ("Live language-model inference" if mode == "llm"
                          else "Deterministic mode: rule-based planning and template/extractive answers; "
                               "no language model was called."),
            },
            "trace": state.get("trace", []),
            "error": state.get("error"),
            "latency_ms": round(latency_ms, 2),
            "created_at": utc_now(),
            "disclaimer": SAFETY_DISCLAIMER,
        }
