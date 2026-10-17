import json

import pytest
from pydantic import ValidationError

from api.v2_schemas import AgentRunResponse
from src.persistence.metadata import record_evaluation


def test_index_provenance_persists_without_source_text(runtime):
    manifest = runtime.db.one("SELECT * FROM retrieval_manifest WHERE index_version=?", (runtime.index.version,))
    rows = runtime.db.all("SELECT * FROM retrieval_metadata WHERE index_version=?", (runtime.index.version,))
    assert manifest["chunk_count"] == runtime.index.stats()["chunks"] == len(rows)
    assert all(r["source_ref"] and r["source_version"] and r["content_hash"] for r in rows)
    assert "text" not in rows[0].keys()
    runtime.index.build()
    assert runtime.db.one("SELECT COUNT(*) AS n FROM retrieval_manifest")["n"] == 1


def test_evaluation_metadata_is_idempotent_and_excludes_case_excerpts(runtime):
    report = {
        "run_at": "2026-10-17T00:00:00Z",
        "dataset_version": "v1",
        "mode": "deterministic",
        "gate": {"passed": True},
        "summary": {"all": {"cases": 1}},
        "cases": [{"answer_excerpt": "private_source_text"}],
        "retrieval": {"per_query": [{"query": "private_source_text"}], "modes": {}, "isolation": {}},
    }
    first = record_evaluation(runtime.db, report)
    assert record_evaluation(runtime.db, report) == first
    rows = runtime.db.all("SELECT * FROM evaluation_run")
    assert len(rows) == 1 and "private_source_text" not in rows[0]["metadata"]
    assert json.loads(rows[0]["metadata"])["gate"]["passed"]


def test_openapi_and_runtime_enforce_agent_response_contract(api_client):
    schema = api_client.get("/openapi.json").json()
    response = schema["paths"]["/api/v2/agent/runs"]["post"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"]["$ref"].endswith("/AgentRunResponse")
    result = api_client.post(
        "/api/v2/agent/runs",
        headers={"X-Demo-Persona": "clinician.demo"},
        json={"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"},
    ).json()
    assert AgentRunResponse.model_validate(result).answer.claims
    result["outcome"] = "invented"
    with pytest.raises(ValidationError):
        AgentRunResponse.model_validate(result)
    result["outcome"] = "answered"
    result["evidence"][0]["source_version"] = None
    with pytest.raises(ValidationError):
        AgentRunResponse.model_validate(result)


def test_v2_invalid_input_uses_error_contract_without_echo(api_client):
    response = api_client.post(
        "/api/v2/agent/runs",
        headers={"X-Demo-Persona": "clinician.demo"},
        json={"patient_id": "private_input", "question": "x"},
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request"
    assert "private_input" not in response.text
