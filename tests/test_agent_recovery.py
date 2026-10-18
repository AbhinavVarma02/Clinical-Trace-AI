"""Real SQLite/LangGraph restart, replay, ownership, and admission tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.agent.graph import build_graph
from src.agent.llm import CircuitBreaker
from src.agent.service import AgentBusy, RunConflict, SessionMismatch
from src.runtime import build_runtime
from src.safety.policy import PERSONAS, AccessContext

CLINICIAN = AccessContext(PERSONAS["clinician.demo"])
RESTRICTED = AccessContext(PERSONAS["clinician.restricted"])


class SimulatedProcessLoss(BaseException):
    pass


def interrupt_after_tool(monkeypatch):
    """Abort actual graph iteration after a real tool, without normal error handling."""
    import src.agent.service as service_module

    class CrashGraph:
        def __init__(self, graph):
            self.graph = graph

        def invoke(self, initial, config):
            events = self.graph.stream(initial, config=config, stream_mode="updates", durability="sync")
            try:
                for event in events:
                    if "act" in event:
                        raise SimulatedProcessLoss()
            finally:
                events.close()

    monkeypatch.setattr(service_module, "build_graph", lambda *a, **kw: CrashGraph(build_graph(*a, **kw)))


def test_restart_resumes_real_checkpoint_without_repeating_completed_tool(tmp_path, monkeypatch):
    import src.agent.graph as graph_module
    import src.agent.service as service_module

    calls = []
    original_tool = graph_module.run_tool

    def counted_tool(ctx, name, args):
        calls.append(name)
        return original_tool(ctx, name, args)

    monkeypatch.setattr(graph_module, "run_tool", counted_tool)
    path = tmp_path / "recovery.db"
    first = build_runtime(path, provider=None, enable_model=False)
    interrupt_after_tool(monkeypatch)
    with pytest.raises(SimulatedProcessLoss):
        first.agent.run(CLINICIAN, "synthetic_p001", "Why was metformin stopped?", run_id="restart-run")
    assert len(calls) == 1
    completed_tool = calls[0]
    assert first.close()

    monkeypatch.setattr(service_module, "build_graph", build_graph)
    second = build_runtime(path, provider=None, enable_model=False)
    try:
        assert second.agent.run_status(CLINICIAN, "restart-run")["status"] == "interrupted"
        with pytest.raises(SessionMismatch):
            second.agent.resume(RESTRICTED, "restart-run")
        result = second.agent.resume(CLINICIAN, "restart-run")
        assert result["outcome"] == "answered"
        assert calls.count(completed_tool) == 1
        assert result["verification"]["claims_supported"] > 0
        assert second.agent.run_status(CLINICIAN, "restart-run")["resume_count"] == 1
        with pytest.raises(RunConflict):
            second.agent.resume(CLINICIAN, "restart-run")
    finally:
        second.close()


def test_restart_rejects_changed_execution_revision(tmp_path, monkeypatch):
    import src.agent.service as service_module

    path = tmp_path / "changed.db"
    first = build_runtime(path, provider=None, enable_model=False)
    interrupt_after_tool(monkeypatch)
    with pytest.raises(SimulatedProcessLoss):
        first.agent.run(CLINICIAN, "synthetic_p001", "metformin history", run_id="changed-run")
    first.close()
    monkeypatch.setattr(service_module, "build_graph", build_graph)
    second = build_runtime(path, provider=None, enable_model=False)
    try:
        second.agent.revision = "changed-code"
        with pytest.raises(RunConflict, match="changed"):
            second.agent.resume(CLINICIAN, "changed-run")
    finally:
        second.close()


def test_idempotent_http_replay_and_owner_only_cancellation(api_client, runtime):
    headers = {"X-Demo-Persona": "clinician.demo", "Idempotency-Key": "replay-key"}
    body = {"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"}
    first = api_client.post("/api/v2/agent/runs", headers=headers, json=body)
    second = api_client.post("/api/v2/agent/runs", headers=headers, json=body)
    assert first.status_code == 200 and second.json() == first.json()
    assert runtime.db.one("SELECT COUNT(*) AS n FROM agent_run")["n"] == 1
    conflict = api_client.post("/api/v2/agent/runs", headers=headers, json={**body, "question": "HbA1c trend"})
    assert conflict.status_code == 409
    run_id = first.json()["run_id"]
    other = {"X-Demo-Persona": "clinician.restricted"}
    assert api_client.post(f"/api/v2/agent/runs/{run_id}/cancel", headers=other).status_code == 403
    assert api_client.post(f"/api/v2/agent/runs/{run_id}/resume", headers=other).status_code == 403
    assert api_client.get(f"/api/v2/agent/runs/{run_id}/status", headers=headers).json()["status"] == "completed"
    assert api_client.post(f"/api/v2/agent/runs/{run_id}/resume", headers=headers).status_code == 409


def test_bounded_admission_and_cooperative_shutdown(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = runtime.agent._execute

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    runtime.agent._slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(runtime.agent, "_execute", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.agent.run, CLINICIAN, "synthetic_p001", "HbA1c trend")
        assert entered.wait(10)
        try:
            with pytest.raises(AgentBusy):
                runtime.agent.run(CLINICIAN, "synthetic_p001", "HbA1c trend")
            assert not runtime.agent.close(grace_s=0)
        finally:
            release.set()
        assert future.result(timeout=15)["outcome"] == "cancelled"
    assert runtime.agent.describe()["active"] == 0


def test_circuit_breaker_permits_one_recovery_probe():
    breaker = CircuitBreaker(threshold=1, cooldown_s=0)
    breaker.record(False)
    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(pool.map(lambda _: breaker.allow(), range(8)))
    assert sum(allowed) == 1
    breaker.record(True)
    assert breaker.allow()
