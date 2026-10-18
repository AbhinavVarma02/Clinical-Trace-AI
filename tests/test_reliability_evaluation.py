"""Offline controls for the repeatable, budget-aware reliability experiment."""

from __future__ import annotations

import json

import httpx
import pytest

from evaluation.reliability import (
    MODEL,
    BudgetStop,
    BudgetTransport,
    citation_checks,
    load_scenarios,
    run,
    source_fingerprint,
)
from src.safety.policy import PERSONAS, AccessContext

ORIGIN = "https://clinical-trace-test.workers.dev"


def status(used=2312.8, cap=8000, **overrides):
    result = {
        "accounting": "ok",
        "edge": {"served_at": "2026-10-18T04:00:00Z"},
        "inference": {
            "provider": "cloudflare-workers-ai",
            "model": MODEL,
            "configured": True,
            "quota_state": "available",
            "neurons_used_today": used,
            "daily_neuron_budget": cap,
        },
    }
    result.update(overrides)
    return result


def completion(raw="{}", neurons=1):
    return {
        "choices": [{"message": {"content": raw}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        "edge": {"estimated_neurons": neurons},
    }


def request(max_tokens=300):
    return httpx.Request(
        "POST",
        f"{ORIGIN}/edge/llm/v1/chat/completions",
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "Tools: mock catalog"}],
        },
    )


def mock_edge(posts, *, snapshot=None, raw="{}", post_status=200):
    def handle(req):
        if req.url.path == "/edge/status":
            return httpx.Response(200, json=snapshot or status())
        if req.url.path == "/edge/metrics":
            return httpx.Response(200, json={"inference": {"calls": 10, "neurons": 2312.8}})
        posts.append(req)
        return httpx.Response(post_status, json=completion(raw))

    return httpx.MockTransport(handle)


def test_six_questions_have_stable_ui_order():
    scenarios = load_scenarios()
    assert [s.id for s in scenarios] == [
        "readmission-journey",
        "contradictory-notes",
        "incomplete-record",
        "anticoagulation",
        "metformin",
        "hba1c",
    ]
    assert scenarios[0].question == "Summarize this patient journey"
    assert len(source_fingerprint()) > 20


def test_offline_runs_are_real_deterministic_and_report_all_attempts(tmp_path):
    output = tmp_path / "offline.json"
    report = run(repetitions=2, output=output)
    assert report["completed"] and report["planned_runs"] == 12
    assert report["summary"]["runs_attempted"] == 12
    assert report["summary"]["llm_composed"] == 0
    assert report["summary"]["citation_errors"] == 0
    assert report["unattempted_runs"] == 0
    assert [r["repetition"] for r in report["runs"]] == [1] * 6 + [2] * 6
    assert all(r["citation_checks"]["valid"] for r in report["runs"])
    assert all(not r["provider_attempts"] for r in report["runs"])
    assert json.loads(output.read_text())["summary"] == report["summary"]
    with pytest.raises(ValueError, match="overwrite"):
        run(repetitions=1, output=output)


def test_selected_guided_workload_repeats_only_selected_questions_in_ui_order():
    report = run(repetitions=2, scenario_ids=["readmission-journey"])
    assert report["completed"] and report["planned_runs"] == 2
    assert [r["id"] for r in report["runs"]] == ["readmission-journey"] * 2
    assert [r["repetition"] for r in report["runs"]] == [1, 2]
    assert [q["id"] for q in report["questions"]] == ["readmission-journey"]
    assert list(report["by_scenario"]) == ["readmission-journey"]
    assert all(r["citation_checks"]["valid"] for r in report["runs"])
    ordered = run(repetitions=1, scenario_ids=["metformin", "readmission-journey"])
    assert [r["id"] for r in ordered["runs"]] == ["readmission-journey", "metformin"]


@pytest.mark.parametrize("scenario_ids", [["unknown"], ["readmission-journey"] * 2, []])
def test_invalid_scenario_selection_fails_before_network_or_runtime(monkeypatch, scenario_ids):
    monkeypatch.setenv("RELIABILITY_TEST_KEY", "test-secret")
    requests = []

    def forbidden_request(request):
        requests.append(request)
        raise AssertionError("Invalid workload must not contact the edge")

    def forbidden_runtime(*args, **kwargs):
        raise AssertionError("Invalid workload must not start the agent")

    monkeypatch.setattr("evaluation.reliability.build_runtime", forbidden_runtime)
    with pytest.raises(ValueError, match="scenario|duplicate"):
        run(
            scenario_ids=scenario_ids,
            edge_origin=ORIGIN,
            api_key_env="RELIABILITY_TEST_KEY",
            max_neurons=1000,
            max_calls=112,
            transport=httpx.MockTransport(forbidden_request),
        )
    assert not requests


def test_live_without_credentials_or_explicit_caps_fails_before_inference(monkeypatch):
    monkeypatch.delenv("RELIABILITY_TEST_KEY", raising=False)
    report = run(repetitions=1, edge_origin=ORIGIN, api_key_env="RELIABILITY_TEST_KEY")
    assert not report["completed"] and not report["runs"]
    assert "credential" in report["stop_reason"]
    monkeypatch.setenv("RELIABILITY_TEST_KEY", "never-print-this-test-secret")
    report = run(repetitions=1, edge_origin=ORIGIN, api_key_env="RELIABILITY_TEST_KEY")
    assert "explicit" in report["stop_reason"]


@pytest.mark.parametrize(
    "snapshot",
    [
        status(used=None),
        status(used=6100),
        status(accounting="unavailable"),
        status(inference={**status()["inference"], "configured": False}),
        status(inference={**status()["inference"], "model": "different-model"}),
        status(inference={**status()["inference"], "quota_state": "exhausted"}),
    ],
)
def test_preflight_fails_closed(snapshot):
    posts = []
    budget = BudgetTransport(ORIGIN, MODEL, 1000, 112, transport=mock_edge(posts, snapshot=snapshot))
    try:
        with pytest.raises(BudgetStop):
            budget.start()
        assert not posts
    finally:
        budget.close()


def test_physical_attempt_cap_and_raw_parse_failure_are_retained():
    posts = []
    budget = BudgetTransport(ORIGIN, MODEL, 1000, 1, transport=mock_edge(posts, raw="{truncated"))
    try:
        budget.start()
        budget.handle_request(request())
        with pytest.raises(BudgetStop, match="request cap"):
            budget.handle_request(request())
        assert len(posts) == len(budget.attempts) == 1
        assert budget.attempts[0]["parse_failure"] == "JSONDecodeError"
        assert budget.attempts[0]["raw_output"] == "{truncated"
        assert budget.spent == 1
    finally:
        budget.close()


def test_neuron_cap_reserves_full_request_before_sending():
    posts = []
    budget = BudgetTransport(ORIGIN, MODEL, 0.1, 112, transport=mock_edge(posts))
    try:
        budget.start()
        with pytest.raises(BudgetStop, match="neuron cap"):
            budget.handle_request(request())
        assert not posts and not budget.attempts
    finally:
        budget.close()


def test_shared_budget_reserve_is_checked_again_before_post():
    posts = []
    calls = []

    def edge(req):
        if req.url.path == "/edge/status":
            calls.append(req)
            return httpx.Response(200, json=status(used=2312.8 if len(calls) == 1 else 5999))
        if req.url.path == "/edge/metrics":
            return httpx.Response(200, json={})
        posts.append(req)
        return httpx.Response(200, json=completion())

    budget = BudgetTransport(ORIGIN, MODEL, 5000, 112, transport=httpx.MockTransport(edge))
    try:
        budget.start()
        with pytest.raises(BudgetStop, match="reserve"):
            budget.handle_request(request())
        assert not posts
    finally:
        budget.close()


def test_quota_failure_stops_without_second_physical_attempt():
    posts = []
    budget = BudgetTransport(ORIGIN, MODEL, 1000, 112, transport=mock_edge(posts, post_status=429))
    try:
        budget.start()
        assert budget.handle_request(request()).status_code == 429
        with pytest.raises(BudgetStop, match="429"):
            budget.handle_request(request())
        assert len(posts) == 1
    finally:
        budget.close()


def test_mocked_live_failures_keep_all_six_runs_and_outputs(monkeypatch, tmp_path):
    secret = "never-print-this-test-secret"
    monkeypatch.setenv("RELIABILITY_TEST_KEY", secret)
    posts = []
    report = run(
        repetitions=1,
        output=tmp_path / "mocked.json",
        edge_origin=ORIGIN,
        api_key_env="RELIABILITY_TEST_KEY",
        max_neurons=1000,
        max_calls=112,
        transport=mock_edge(posts, raw="{truncated"),
    )
    assert report["completed"]
    assert len(report["runs"]) == 6 and len(posts) == 12
    assert report["summary"]["deterministic_fallbacks"] == 6
    assert report["summary"]["composer_parse_failures"] == 6
    assert report["summary"]["parse_failures"] == 12
    assert all(r["llm_counts"]["drafted"] == 0 for r in report["runs"])
    assert all(r["result"]["answer"]["claims"] for r in report["runs"])
    assert secret not in json.dumps(report)
    assert all(req.headers["Authorization"] == f"Bearer {secret}" for req in posts)


def test_stopped_live_run_records_fallback_and_unattempted_count(monkeypatch):
    monkeypatch.setenv("RELIABILITY_TEST_KEY", "test-secret")
    posts = []
    report = run(
        repetitions=2,
        edge_origin=ORIGIN,
        api_key_env="RELIABILITY_TEST_KEY",
        max_neurons=1000,
        max_calls=1,
        transport=mock_edge(posts),
    )
    assert not report["completed"]
    assert report["summary"]["runs_attempted"] == 1 and report["unattempted_runs"] == 11
    assert report["budget"]["physical_requests"] == 1
    assert "cap" in report["stop_reason"]


def test_quota_on_final_planned_run_is_incomplete(monkeypatch):
    monkeypatch.setenv("RELIABILITY_TEST_KEY", "test-secret")
    posts = []
    normal_edge = mock_edge(posts)

    def final_quota(req):
        response = normal_edge.handle_request(req)
        if len(posts) == 12 and req.method == "POST":
            return httpx.Response(429, json={"error": {"code": "quota_exhausted"}})
        return response

    report = run(
        repetitions=1,
        edge_origin=ORIGIN,
        api_key_env="RELIABILITY_TEST_KEY",
        max_neurons=1000,
        max_calls=112,
        transport=httpx.MockTransport(final_quota),
    )
    assert not report["completed"] and report["unattempted_runs"] == 0
    assert report["summary"]["runs_attempted"] == 6
    assert "429" in report["stop_reason"]
    assert "metrics" in report["postflight"]


def test_citation_version_patient_and_missing_reference_checks(runtime):
    access = AccessContext(PERSONAS["clinician.demo"])
    result = runtime.agent.run(access, "synthetic_p001", "Why was metformin stopped?")
    assert citation_checks(runtime, result)["valid"]
    citation = result["answer"]["claims"][0]["citations"][0]
    evidence = next(e for e in result["evidence"] if e["evidence_id"] == citation)
    evidence["source_version"] = "999"
    assert citation_checks(runtime, result)["invalid"] > 0
    evidence["patient_id"] = "synthetic_p002"
    assert "another patient" in citation_checks(runtime, result)["citations"][0]["reason"]
    result["answer"]["claims"][0]["citations"] = ["unknown@v1"]
    assert "absent" in citation_checks(runtime, result)["citations"][0]["reason"]
