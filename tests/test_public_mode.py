"""Public-demo hardening: per-visitor workspace isolation, edge-only access and quota degradation."""

from __future__ import annotations

import json

import httpx
import pytest

from src.agent.llm import OpenAICompatibleProvider, ProviderQuotaExhausted, ProviderUnavailable
from src.agent.service import AgentService
from src.safety.policy import PERSONAS, AccessContext, display_principal, in_scope

WS_A = "workspaceAAAAAAAAAAAAAAAA1"
WS_B = "workspaceBBBBBBBBBBBBBBBB2"


def _headers(persona: str, workspace: str | None) -> dict[str, str]:
    headers = {"X-Demo-Persona": persona}
    if workspace is not None:
        headers["X-Demo-Workspace"] = workspace
    return headers


def _answered_run(api_client, workspace: str) -> dict:
    response = api_client.post(
        "/api/v2/agent/runs",
        headers=_headers("clinician.demo", workspace),
        json={"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"},
    )
    assert response.status_code == 200
    run = response.json()
    assert run["outcome"] == "answered"
    return run


def test_principal_never_exposes_raw_workspace() -> None:
    ctx = AccessContext(PERSONAS["clinician.demo"], workspace=WS_A)
    assert ctx.principal.startswith("clinician.demo@") and WS_A not in ctx.principal
    assert display_principal(ctx.principal) == "clinician.demo"
    assert in_scope(ctx, ctx.principal)
    assert not in_scope(AccessContext(PERSONAS["clinician.demo"], workspace=WS_B), ctx.principal)
    assert not in_scope(AccessContext(PERSONAS["clinician.demo"]), ctx.principal)


def test_runs_are_invisible_across_workspaces(api_client) -> None:
    run = _answered_run(api_client, WS_A)
    mine = api_client.get("/api/v2/agent/recent", headers=_headers("clinician.demo", WS_A)).json()
    theirs = api_client.get("/api/v2/agent/recent", headers=_headers("clinician.demo", WS_B)).json()
    unscoped = api_client.get("/api/v2/agent/recent", headers=_headers("clinician.demo", None)).json()
    assert [r["run_id"] for r in mine] == [run["run_id"]]
    assert theirs == [] and unscoped == []
    for headers in (_headers("clinician.demo", WS_B), _headers("reviewer.demo", WS_B)):
        assert api_client.get(f"/api/v2/agent/runs/{run['run_id']}", headers=headers).status_code == 404
    other_session = api_client.post(
        "/api/v2/agent/runs",
        headers=_headers("clinician.demo", WS_B),
        json={"patient_id": "synthetic_p001", "question": "summary", "session_id": run["session_id"]},
    )
    assert other_session.status_code == 403


def test_review_workflow_is_scoped_to_the_visitor_workspace(api_client) -> None:
    run = _answered_run(api_client, WS_A)
    review = api_client.post(
        "/api/v2/reviews", headers=_headers("clinician.demo", WS_A), json={"run_id": run["run_id"]}
    ).json()
    assert review["submitted_by"] == "clinician.demo"
    assert api_client.get("/api/v2/reviews", headers=_headers("reviewer.demo", WS_B)).json() == []
    assert (
        api_client.get(f"/api/v2/reviews/{review['review_id']}", headers=_headers("reviewer.demo", WS_B)).status_code
        == 404
    )
    blocked = api_client.post(
        f"/api/v2/reviews/{review['review_id']}/decision",
        headers=_headers("reviewer.demo", WS_B),
        json={"decision": "approved", "artifact_hash": review["artifact_hash"]},
    )
    assert blocked.status_code == 404
    listed = api_client.get("/api/v2/reviews", headers=_headers("reviewer.demo", WS_A)).json()
    assert [r["review_id"] for r in listed] == [review["review_id"]]
    decided = api_client.post(
        f"/api/v2/reviews/{review['review_id']}/decision",
        headers=_headers("reviewer.demo", WS_A),
        json={"decision": "approved", "artifact_hash": review["artifact_hash"]},
    ).json()
    assert decided["status"] == "approved" and decided["approval_valid"] is True
    assert {e["actor"] for e in decided["events"]} == {"clinician.demo", "reviewer.demo"}
    self_review = api_client.post(
        f"/api/v2/reviews/{review['review_id']}/decision",
        headers=_headers("clinician.demo", WS_A),
        json={"decision": "approved", "artifact_hash": review["artifact_hash"]},
    )
    assert self_review.status_code in (403, 409)


def test_audit_log_is_scoped_and_hides_workspace_tags(api_client) -> None:
    _answered_run(api_client, WS_A)
    other = api_client.get("/api/v2/audit", headers=_headers("admin.demo", WS_B)).json()
    assert all(e["persona"] == "admin.demo" for e in other["events"])
    own = api_client.get("/api/v2/audit", headers=_headers("admin.demo", WS_A)).json()
    assert any(e["persona"] == "clinician.demo" for e in own["events"])
    assert all("@" not in e["persona"] for e in own["events"])


def test_public_mode_requires_a_well_formed_workspace(api_client, monkeypatch) -> None:
    monkeypatch.setenv("CLINICAL_TRACE_PUBLIC_MODE", "1")
    missing = api_client.get("/api/v2/patients", headers=_headers("clinician.demo", None))
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "workspace_required"
    bad = api_client.get("/api/v2/patients", headers=_headers("clinician.demo", "short"))
    assert bad.status_code == 400
    assert api_client.get("/api/v2/patients", headers=_headers("clinician.demo", WS_A)).status_code == 200
    assert api_client.get("/api/v2/system/status").status_code == 200


def test_edge_secret_gates_protected_routes(api_client, monkeypatch) -> None:
    monkeypatch.setenv("CLINICAL_TRACE_EDGE_SECRET", "s" * 40)
    headers = _headers("clinician.demo", WS_A)
    denied = api_client.get("/api/v2/patients", headers=headers)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "edge_required"
    wrong = api_client.get("/api/v2/patients", headers={**headers, "X-Edge-Secret": "x" * 40})
    assert wrong.status_code == 403
    assert api_client.get("/api/v2/system/status").status_code == 200  # container health check stays open
    allowed = api_client.get(
        "/api/v2/patients", headers={**headers, "X-Edge-Secret": "s" * 40, "X-Edge-Client": "ab" * 12}
    )
    assert allowed.status_code == 200
    assert api_client.get("/fhir/metadata").status_code == 403


def _provider(handler, calls: list) -> OpenAICompatibleProvider:
    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return handler(request)

    return OpenAICompatibleProvider(
        "http://edge/v1",
        "@cf/qwen/qwen3-30b-a3b-fp8",
        "cloudflare-workers-ai",
        transport=httpx.MockTransport(wrapped),
        max_retries=2,
    )


def test_quota_exhaustion_fails_fast_without_retry_or_breaker_trip() -> None:
    calls: list = []
    provider = _provider(
        lambda r: httpx.Response(
            429, headers={"Retry-After": "120"}, json={"error": {"code": "quota_exhausted", "message": "used"}}
        ),
        calls,
    )
    with pytest.raises(ProviderQuotaExhausted) as raised:
        provider.complete("system", "user")
    assert len(calls) == 1 and raised.value.retry_after_s == 120
    with pytest.raises(ProviderQuotaExhausted):
        provider.complete("system", "user")
    assert len(calls) == 1  # no network call while the allocation is known to be exhausted
    described = provider.describe()
    assert described["quota_state"] == "exhausted" and described["circuit"] == "closed"
    assert described["quota_rejections"] == 2 and described["estimated_cost_usd"] == 0.0


def test_client_errors_are_not_retried() -> None:
    calls: list = []
    provider = _provider(lambda r: httpx.Response(401, json={"error": {"code": "unauthorized"}}), calls)
    with pytest.raises(ProviderUnavailable):
        provider.complete("system", "user")
    assert len(calls) == 1


def test_reported_neurons_are_accounted() -> None:
    calls: list = []
    provider = _provider(
        lambda r: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "neurons": 1.5},
            },
        ),
        calls,
    )
    provider.complete("s", "u")
    provider.complete("s", "u")
    assert provider.describe()["neurons"] == 3.0


def test_agent_run_is_labelled_when_free_quota_is_exhausted(runtime) -> None:
    calls: list = []
    provider = _provider(lambda r: httpx.Response(429, json={"error": {"code": "quota_exhausted"}}), calls)
    service = AgentService(runtime.db, runtime.records, runtime.index, runtime.policy, provider=provider)
    ctx = AccessContext(PERSONAS["clinician.demo"], workspace=WS_A)
    result = service.run(ctx, "synthetic_p001", "How has HbA1c changed over time?")
    assert result["outcome"] == "answered"
    assert result["inference"]["degraded_reason"] == "quota_exhausted"
    assert "daily free inference allocation" in result["inference"]["label"]
    assert result["inference"]["planner"] == "deterministic-fallback"
    assert len(calls) == 1  # composer did not call the provider again after the planner learned of exhaustion
