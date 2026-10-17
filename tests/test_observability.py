import json

import pytest

from src import observability


def test_local_spans_correlate_and_exclude_source_text(api_client, tmp_path):
    path = tmp_path / "spans.jsonl"
    observability.configure(path)
    try:
        question = "Why was metformin stopped?"
        response = api_client.post(
            "/api/v2/agent/runs",
            headers={"X-Demo-Persona": "clinician.demo"},
            json={"patient_id": "synthetic_p001", "question": question},
        )
        assert response.status_code == 200
        assert response.json()["outcome"] == "answered"
        trace_id = response.headers["x-trace-id"]
        with pytest.raises(ValueError), observability.span("failure", {"question": "sensitive-text"}):
            raise ValueError("sensitive-exception")
    finally:
        observability.shutdown()
    raw = path.read_text()
    assert question not in raw and "sensitive-text" not in raw and "sensitive-exception" not in raw
    assert "declining eGFR" not in raw
    spans = [json.loads(line) for line in raw.splitlines()]
    root = next(s for s in spans if s["name"] == "clinical.http")
    nodes = [s for s in spans if s["name"] == "clinical.agent.node"]
    assert nodes and all(s["context"]["trace_id"].endswith(trace_id) for s in nodes)
    assert root["attributes"]["http.route"] == "/api/v2/agent/runs"
    assert root["attributes"]["request_id"] == response.headers["x-request-id"]
    assert next(s for s in spans if s["name"] == "failure")["status"]["status_code"] == "ERROR"
