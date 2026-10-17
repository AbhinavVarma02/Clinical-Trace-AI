"""LangGraph orchestration for the Clinical Case Review Agent.

Graph (explicit state, conditional routing, bounded loops)::

    guard --refuse--> finalize
      |
    plan -> act (one tool per step, loops until plan done or budget hit)
      |
    compose -> verify --failed & LLM--> compose (deterministic fallback, once)
      |
    review_gate -> finalize

Budgets: at most ``MAX_TOOL_CALLS`` tool calls and a LangGraph recursion limit.
Cancellation is checked at every node. The service supplies a durable SQLite
saver; direct graph callers may use the default in-memory saver.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from src.agent.composer import Draft, compose_deterministic, compose_llm
from src.agent.llm import OpenAICompatibleProvider
from src.agent.planner import MAX_TOOL_CALLS, Plan, deterministic_plan, detect_intents, llm_plan
from src.agent.tools import ToolContext, ToolResult, run_tool
from src.safety.guards import GuardResult, classify_question
from src.safety.verifier import EvidenceItem, verify_claims
from src.observability import span as telemetry_span

RECURSION_LIMIT = 40


class AgentState(TypedDict, total=False):
    run_id: str
    question: str
    patient_id: str
    other_patient_markers: tuple[str, ...]
    guard: GuardResult
    plan: Plan
    step: int
    results: dict[str, ToolResult]
    evidence: dict[str, EvidenceItem]
    draft: Draft
    verification: dict[str, Any]
    compose_attempts: int
    fallback_used: bool
    llm_attempt: dict[str, Any]
    review: dict[str, Any]
    outcome: str
    trace: list[dict[str, Any]]


class CancelledRun(RuntimeError):
    pass


class _Cancellation:
    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register(self, run_id: str) -> threading.Event:
        with self._lock:
            return self._events.setdefault(run_id, threading.Event())

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            event = self._events.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def release(self, run_id: str) -> None:
        with self._lock:
            self._events.pop(run_id, None)


CANCELLATION = _Cancellation()


def build_graph(ctx: ToolContext, provider: OpenAICompatibleProvider | None,
                checkpointer: Any = None, check_cancel: Callable[[], bool] | None = None,
                observer: Callable[[dict[str, Any]], None] | None = None) -> Any:
    """Compile a graph bound to one access context (session isolation by construction)."""

    def traced(name: str, fn: Callable[[AgentState], dict[str, Any]]) -> Callable[[AgentState], dict[str, Any]]:
        def node(state: AgentState) -> dict[str, Any]:
            event = CANCELLATION.register(state["run_id"])
            if event.is_set() or (check_cancel is not None and check_cancel()):
                raise CancelledRun(state["run_id"])
            started = time.perf_counter()
            with telemetry_span("clinical.agent.node", {"run_id": state["run_id"], "node": name}) as current:
                update = fn(state)
                details = update.get("_span", {})
                if current is not None:
                    for key in ("tool", "ok", "error_type"):
                        if details.get(key) is not None:
                            current.set_attribute(key, details[key])
            span = {"node": name, "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
            span.update(update.pop("_span", {}))
            update["trace"] = [*state.get("trace", []), span]
            if observer is not None:
                observer({"run_id": state["run_id"], **{k: v for k, v in span.items()
                                                       if k in {"node", "duration_ms", "tool", "ok", "error_type"}}})
            return update
        return node

    def guard(state: AgentState) -> dict[str, Any]:
        result = classify_question(state["question"], state["patient_id"], state.get("other_patient_markers", ()))
        return {"guard": result, "_span": {"verdict": result.verdict, "category": result.category}}

    def plan(state: AgentState) -> dict[str, Any]:
        chosen = llm_plan(state["question"], provider) if provider else deterministic_plan(state["question"])
        return {"plan": chosen, "step": 0, "results": {}, "evidence": {},
                "_span": {"source": chosen.source, "tools": [c["name"] for c in chosen.calls],
                          "intents": chosen.intents}}

    def act(state: AgentState) -> dict[str, Any]:
        call = state["plan"].calls[state["step"]]
        result = run_tool(ctx, call["name"], call["args"])
        evidence = dict(state.get("evidence", {}))
        for item in result.evidence:
            existing = evidence.get(item.evidence_id)
            if existing is None:
                evidence[item.evidence_id] = item
            elif item.text not in existing.text:
                existing.text = f"{existing.text} | {item.text}"
        results = {**state.get("results", {}), call["name"]: result}
        return {"results": results, "evidence": evidence, "step": state["step"] + 1, "_span": result.summary()}

    def compose(state: AgentState) -> dict[str, Any]:
        attempts = state.get("compose_attempts", 0)
        use_llm = provider is not None and attempts == 0
        if use_llm:
            draft = compose_llm(state["question"], state.get("evidence", {}), provider)  # type: ignore[arg-type]
        else:
            intents = state["plan"].intents if state.get("plan") else detect_intents(state["question"])
            draft = compose_deterministic(state["question"], intents, state.get("results", {}))
        return {"draft": draft, "compose_attempts": attempts + 1,
                "fallback_used": state.get("fallback_used", False) or (provider is not None and not use_llm),
                "_span": {"composer": draft.composer, "claims": len(draft.claims)}}

    def verify(state: AgentState) -> dict[str, Any]:
        draft = state["draft"]
        report = verify_claims(draft.claims, state.get("evidence", {}), state["patient_id"])
        if draft.summary:
            cited = sorted({c for claim in report["accepted"] for c in claim["citations"]})
            summary_check = verify_claims([{"text": draft.summary, "citations": cited}],
                                          state.get("evidence", {}), state["patient_id"])
            report["summary_verified"] = summary_check["status"] == "verified"
            if not report["summary_verified"]:
                report["summary_rejection"] = summary_check["rejected"][0]["reasons"] if summary_check[
                    "rejected"] else []
        update: dict[str, Any] = {
            "verification": report,
            "_span": {"status": report["status"], "accepted": report["claims_supported"],
                      "rejected": len(report["rejected"])},
        }
        if draft.composer.startswith("llm"):
            update["llm_attempt"] = {
                "composer": draft.composer,
                "status": report["status"],
                "claims_total": report["claims_total"],
                "claims_supported": report["claims_supported"],
                "rejected": [{"text": r["text"], "reasons": r["reasons"]} for r in report["rejected"]],
                "abstain_reason": draft.abstain_reason,
                "notes": draft.notes,
                "latency_ms": draft.latency_ms,
            }
        return update

    def review_gate(state: AgentState) -> dict[str, Any]:
        reasons = []
        report = state.get("verification") or {}
        if report.get("status") == "partially_verified":
            reasons.append("some generated claims failed verification and were removed")
        inconsistencies = state.get("results", {}).get("detect_inconsistencies")
        if inconsistencies is not None and inconsistencies.ok and inconsistencies.data:
            reasons.append("the record contains inconsistencies")
        if any(e.flags for e in state.get("evidence", {}).values()):
            reasons.append("retrieved content contained instruction-like text")
        accepted_kinds = {c.get("kind") for c in report.get("accepted", [])}
        if accepted_kinds & {"risk", "risk_explanation"}:
            reasons.append("the answer includes a model risk estimate")
        attempt = state.get("llm_attempt") or {}
        if state.get("fallback_used") and attempt.get("claims_total"):
            reasons.append("the LLM answer failed verification and a deterministic answer was used")
        review = {"required": bool(reasons), "reasons": reasons}
        return {"review": review, "_span": {"required": review["required"]}}

    def finalize(state: AgentState) -> dict[str, Any]:
        guard_result = state.get("guard")
        if guard_result is not None and not guard_result.allowed:
            outcome = "refused"
        elif state.get("verification", {}).get("claims_supported", 0) > 0:
            outcome = "answered"
        else:
            outcome = "abstained"
        return {"outcome": outcome, "_span": {"outcome": outcome}}

    def after_guard(state: AgentState) -> str:
        return "plan" if state["guard"].allowed else "finalize"

    def after_plan_or_act(state: AgentState) -> str:
        plan_obj = state["plan"]
        if state["step"] < len(plan_obj.calls) and state["step"] < MAX_TOOL_CALLS:
            return "act"
        return "compose"

    def after_verify(state: AgentState) -> str:
        if (state["verification"]["status"] == "failed" and provider is not None
                and state.get("compose_attempts", 0) < 2):
            return "compose"
        return "review_gate"

    graph = StateGraph(AgentState)
    for name, fn in (("guard", guard), ("plan", plan), ("act", act), ("compose", compose), ("verify", verify),
                     ("review_gate", review_gate), ("finalize", finalize)):
        graph.add_node(name, traced(name, fn))
    graph.add_edge(START, "guard")
    graph.add_conditional_edges("guard", after_guard, {"plan": "plan", "finalize": "finalize"})
    graph.add_conditional_edges("plan", after_plan_or_act, {"act": "act", "compose": "compose"})
    graph.add_conditional_edges("act", after_plan_or_act, {"act": "act", "compose": "compose"})
    graph.add_edge("compose", "verify")
    graph.add_conditional_edges("verify", after_verify, {"compose": "compose", "review_gate": "review_gate"})
    graph.add_edge("review_gate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())
