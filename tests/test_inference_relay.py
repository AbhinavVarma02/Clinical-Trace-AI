"""Edge inference relay: completions travel over the streamed run instead of outbound calls."""

from __future__ import annotations

import json
import threading

import pytest

from src.agent.llm import ProviderUnavailable
from src.agent.relay import RELAY_CHANNEL, RelayChannelUnavailable, RelayHub, relay_provider
from src.agent.service import AgentService
from src.safety.policy import PERSONAS, AccessContext

SECRET = "s" * 40
WS = "relayworkspace000000000001"


def _completion(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "neurons": 0.25},
    }


def test_relayed_completion_returns_through_the_provider() -> None:
    hub = RelayHub()
    provider = relay_provider("@cf/qwen/qwen3-30b-a3b-fp8", timeout_s=5, hub=hub)
    seen: list[dict] = []

    def channel(frame: dict) -> None:
        seen.append(frame)
        threading.Thread(target=hub.fulfil, args=(frame["relay_id"], 200, _completion('{"ok": true}'))).start()

    token = RELAY_CHANNEL.set(channel)
    try:
        completion = provider.complete("system", "user", max_tokens=50)
    finally:
        RELAY_CHANNEL.reset(token)
    assert completion.text == '{"ok": true}'
    assert seen[0]["request"]["messages"][1]["content"] == "user"
    assert seen[0]["request"]["model"] == "@cf/qwen/qwen3-30b-a3b-fp8"
    described = provider.describe()
    assert described["provider"] == "cloudflare-workers-ai" and described["neurons"] == 0.25
    assert hub.pending() == 0


def test_without_a_relay_channel_the_provider_refuses_without_tripping_the_breaker() -> None:
    provider = relay_provider("@cf/qwen/qwen3-30b-a3b-fp8", timeout_s=1, hub=RelayHub())
    for _ in range(5):
        with pytest.raises(RelayChannelUnavailable):
            provider.complete("s", "u")
    assert provider.breaker.state == "closed" and provider.calls == 0


def test_unanswered_relay_times_out_as_a_provider_failure() -> None:
    hub = RelayHub()
    provider = relay_provider("@cf/qwen/qwen3-30b-a3b-fp8", timeout_s=0.2, hub=hub)
    token = RELAY_CHANNEL.set(lambda frame: None)
    try:
        with pytest.raises(ProviderUnavailable):
            provider.complete("s", "u")
    finally:
        RELAY_CHANNEL.reset(token)
    assert provider.failures >= 1 and hub.pending() == 0


def test_quota_exhaustion_relayed_from_the_edge_is_respected() -> None:
    hub = RelayHub()
    provider = relay_provider("@cf/qwen/qwen3-30b-a3b-fp8", timeout_s=5, hub=hub)

    def channel(frame: dict) -> None:
        threading.Thread(
            target=hub.fulfil, args=(frame["relay_id"], 429, {"error": {"code": "quota_exhausted"}})
        ).start()

    token = RELAY_CHANNEL.set(channel)
    try:
        with pytest.raises(ProviderUnavailable):
            provider.complete("s", "u")
    finally:
        RELAY_CHANNEL.reset(token)
    assert provider.quota_state == "exhausted"


def test_non_streamed_runs_are_labelled_when_relay_is_unavailable(runtime) -> None:
    provider = relay_provider("@cf/qwen/qwen3-30b-a3b-fp8", timeout_s=1, hub=RelayHub())
    service = AgentService(runtime.db, runtime.records, runtime.index, runtime.policy, provider=provider)
    result = service.run(
        AccessContext(PERSONAS["clinician.demo"], workspace=WS), "synthetic_p001", "How has HbA1c changed over time?"
    )
    assert result["outcome"] == "answered"
    assert result["inference"]["degraded_reason"] == "relay_unavailable"
    assert "streamed requests through the public edge" in result["inference"]["label"]


def test_relay_endpoint_requires_configuration_and_a_pending_id(api_client, monkeypatch) -> None:
    body = {"status": 200, "body": _completion("{}")}
    assert api_client.post("/api/v2/agent/relay/abcdefghijklmnopqrstuv", json=body).status_code == 404
    monkeypatch.setenv("CLINICAL_TRACE_EDGE_SECRET", SECRET)
    assert api_client.post("/api/v2/agent/relay/abcdefghijklmnopqrstuv", json=body).status_code == 403
    response = api_client.post(
        "/api/v2/agent/relay/abcdefghijklmnopqrstuv", json=body, headers={"X-Edge-Secret": SECRET}
    )
    assert response.status_code == 404


def test_streamed_run_relays_planner_and_composer_calls_through_the_edge(runtime, monkeypatch) -> None:
    """A real server streams relay frames; the test plays the edge and posts completions back."""
    import socket
    import time

    import httpx
    import uvicorn

    from api.main import app
    from api.middleware import RATE_LIMITER
    from src.agent import relay as relay_module

    RATE_LIMITER._buckets.clear()
    monkeypatch.setenv("CLINICAL_TRACE_EDGE_SECRET", SECRET)
    runtime.agent.provider = relay_provider("@cf/qwen/qwen3-30b-a3b-fp8", timeout_s=10, hub=relay_module.HUB)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    headers = {"X-Demo-Persona": "clinician.demo", "X-Demo-Workspace": WS, "X-Edge-Secret": SECRET}
    plan = json.dumps({"tool_calls": [{"name": "get_medication_history", "args": {}}]})
    relays, kinds, result = 0, [], None
    try:
        with httpx.Client(base_url=base, timeout=30) as client:
            with client.stream(
                "POST",
                "/api/v2/agent/runs/stream",
                headers=headers,
                json={"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"},
            ) as stream:
                kind = None
                for line in stream.iter_lines():
                    if line.startswith("event: "):
                        kind = line[7:]
                        kinds.append(kind)
                    elif line.startswith("data: ") and kind == "relay":
                        frame = json.loads(line[6:])
                        system = frame["request"]["messages"][0]["content"].lower()
                        content = plan if "select tools" in system else json.dumps({"claims": [], "summary": None})
                        relays += 1
                        ack = client.post(
                            f"/api/v2/agent/relay/{frame['relay_id']}",
                            headers={"X-Edge-Secret": SECRET},
                            json={"status": 200, "body": _completion(content)},
                        )
                        assert ack.status_code == 200 and ack.json() == {"accepted": True}
                    elif line.startswith("data: ") and kind == "result":
                        result = json.loads(line[6:])
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert relays >= 1 and "progress" in kinds
    assert result is not None and result["outcome"] == "answered"
    assert result["inference"]["mode"] == "llm" and result["inference"]["planner"] == "llm"
    assert result["inference"]["provider"]["failures"] == 0
