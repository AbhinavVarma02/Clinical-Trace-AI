import json


def events(response):
    output = []
    for frame in response.text.split("\n\n"):
        if frame.startswith("event: "):
            kind, data = frame.split("\ndata: ", 1)
            output.append((kind.removeprefix("event: "), json.loads(data)))
    return output


def test_streams_real_progress_before_verified_answer(api_client):
    response = api_client.post(
        "/api/v2/agent/runs/stream",
        headers={"X-Demo-Persona": "clinician.demo"},
        json={"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"},
    )
    assert response.status_code == 200 and "text/event-stream" in response.headers["content-type"]
    frames = events(response)
    assert frames[0][0] == "progress" and frames[-1][0] == "result"
    progress = [data for kind, data in frames if kind == "progress"]
    assert all(set(p) <= {"run_id", "node", "duration_ms", "tool", "ok", "error_type"} for p in progress)
    assert any(p["node"] == "verify" for p in progress)
    result = frames[-1][1]
    assert result["outcome"] == "answered" and result["verification"]["claims_supported"] > 0
    assert all(e["patient_id"] == "synthetic_p001" for e in result["evidence"])


def test_stream_authorization_and_capacity_preserve_http_errors(api_client, runtime):
    headers = {"X-Demo-Persona": "clinician.restricted"}
    body = {"patient_id": "synthetic_p002", "question": "Summarize patient journey"}
    assert api_client.post("/api/v2/agent/runs/stream", headers=headers, json=body).status_code == 403
    permits = [runtime.agent._slots.acquire(blocking=False) for _ in range(runtime.agent.capacity)]
    assert all(permits)
    try:
        response = api_client.post(
            "/api/v2/agent/runs/stream", headers=headers, json={**body, "patient_id": "synthetic_p001"}
        )
        assert response.status_code == 503 and response.json()["error"]["code"] == "agent_busy"
    finally:
        for _ in permits:
            runtime.agent._slots.release()
