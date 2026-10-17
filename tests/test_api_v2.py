"""HTTP contract tests for /api/v2 and the mock FHIR server."""

from __future__ import annotations

from tests.conftest import persona_headers

CLINICIAN = persona_headers("clinician.demo")
RESTRICTED = persona_headers("clinician.restricted")
REVIEWER = persona_headers("reviewer.demo")
ADMIN = persona_headers("admin.demo")


def test_public_status_has_no_patient_data(api_client) -> None:
    body = api_client.get("/api/v2/system/status").json()
    assert body["status"] == "ready" and body["data"]["patients"] == 4
    assert "synthetic_p001" not in str(body)


def test_missing_or_unknown_persona_is_rejected(api_client) -> None:
    for headers in ({}, persona_headers("root")):
        response = api_client.get("/api/v2/patients", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "persona_required"
        assert response.json()["error"]["request_id"]


def test_patient_list_is_scoped_to_panel(api_client) -> None:
    assert len(api_client.get("/api/v2/patients", headers=CLINICIAN).json()) == 4
    assert [p["patient_id"] for p in api_client.get("/api/v2/patients", headers=RESTRICTED).json()] == [
        "synthetic_p001"]


def test_cross_panel_access_fails_closed(api_client) -> None:
    for path in ("/api/v2/patients/synthetic_p002", "/api/v2/patients/synthetic_p002/timeline",
                 "/api/v2/patients/synthetic_p002/documents/p002-d1", "/api/v2/patients/synthetic_p002/risk"):
        response = api_client.get(path, headers=RESTRICTED)
        assert response.status_code == 403, path
    response = api_client.post("/api/v2/agent/runs", headers=RESTRICTED,
                               json={"patient_id": "synthetic_p002", "question": "summary"})
    assert response.status_code == 403


def test_document_from_another_patient_is_not_found(api_client) -> None:
    response = api_client.get("/api/v2/patients/synthetic_p001/documents/p002-d1", headers=CLINICIAN)
    assert response.status_code == 404


def test_patient_detail_and_document(api_client) -> None:
    detail = api_client.get("/api/v2/patients/synthetic_p001", headers=CLINICIAN).json()
    assert detail["profile"]["patient_id"] == "synthetic_p001"
    assert {"encounters", "medications", "lab_trends", "documents"} <= set(detail)
    doc = api_client.get("/api/v2/patients/synthetic_p001/documents/p001-d3", headers=CLINICIAN).json()
    assert "metformin was stopped" in doc["text"]
    assert doc["provenance"]["generator_source"].startswith("urn:clinical-trace:synthetic-generator")
    assert doc["chunks"]


def test_risk_endpoint_reports_unavailability(api_client) -> None:
    body = api_client.get("/api/v2/patients/synthetic_p002/risk", headers=CLINICIAN).json()
    assert body["available"] is False and body["reasons"]


def test_retrieval_endpoint(api_client) -> None:
    body = api_client.post("/api/v2/patients/synthetic_p004/retrieve", headers=CLINICIAN,
                           json={"query": "INR above target", "k": 3}).json()
    assert body["results"] and all(r["patient_id"] == "synthetic_p004" for r in body["results"])
    assert body["diagnostics"]["fusion"].startswith("reciprocal-rank-fusion")


def test_agent_run_contract_and_persistence(api_client) -> None:
    run = api_client.post("/api/v2/agent/runs", headers=CLINICIAN,
                          json={"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"}).json()
    assert run["outcome"] == "answered"
    for key in ("run_id", "session_id", "answer", "evidence", "tool_calls", "verification", "review",
                "inference", "trace", "disclaimer"):
        assert key in run
    fetched = api_client.get(f"/api/v2/agent/runs/{run['run_id']}", headers=CLINICIAN).json()
    assert fetched["run_id"] == run["run_id"]
    assert api_client.get(f"/api/v2/agent/runs/{run['run_id']}", headers=RESTRICTED).status_code == 403
    recent = api_client.get("/api/v2/agent/recent", headers=CLINICIAN).json()
    assert recent[0]["run_id"] == run["run_id"]


def test_trace_contains_no_record_text(api_client) -> None:
    run = api_client.post("/api/v2/agent/runs", headers=CLINICIAN,
                          json={"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"}).json()
    assert "declined to 44" not in str(run["trace"])


def test_invalid_agent_request(api_client) -> None:
    response = api_client.post("/api/v2/agent/runs", headers=CLINICIAN,
                               json={"patient_id": "patient-1", "question": "x"})
    assert response.status_code == 422


def test_review_workflow_binds_to_artifact_hash(api_client) -> None:
    run = api_client.post("/api/v2/agent/runs", headers=CLINICIAN,
                          json={"patient_id": "synthetic_p001", "question": "How has HbA1c changed?"}).json()
    review = api_client.post("/api/v2/reviews", headers=CLINICIAN, json={"run_id": run["run_id"]}).json()
    assert review["status"] == "pending_review"
    rid, digest = review["review_id"], review["artifact_hash"]

    # The submitter cannot approve their own artifact (and clinicians lack review:decide).
    assert api_client.post(f"/api/v2/reviews/{rid}/decision", headers=CLINICIAN,
                           json={"decision": "approved", "artifact_hash": digest}).status_code == 403
    # A stale hash is rejected.
    stale = api_client.post(f"/api/v2/reviews/{rid}/decision", headers=REVIEWER,
                            json={"decision": "approved", "artifact_hash": "f" * 64})
    assert stale.status_code == 409
    # Rejection requires a comment.
    assert api_client.post(f"/api/v2/reviews/{rid}/decision", headers=REVIEWER,
                           json={"decision": "rejected", "artifact_hash": digest}).status_code == 400
    approved = api_client.post(f"/api/v2/reviews/{rid}/decision", headers=REVIEWER,
                               json={"decision": "approved", "artifact_hash": digest}).json()
    assert approved["status"] == "approved" and approved["approval_valid"]

    # Revising the artifact supersedes the approval and opens a new review.
    revised = api_client.post(f"/api/v2/reviews/{rid}/revise", headers=CLINICIAN,
                              json={"remove_claims": [1], "note": "trimmed"}).json()
    assert revised["artifact_version"] == 2 and revised["status"] == "pending_review"
    old = api_client.get(f"/api/v2/reviews/{rid}", headers=REVIEWER).json()
    assert old["status"] == "superseded" and not old["approval_valid"]
    assert [e["action"] for e in old["events"]] == ["submitted", "decided", "superseded"]


def test_admin_sees_ops_and_audit_but_no_patient_data(api_client) -> None:
    api_client.get("/api/v2/patients/synthetic_p002", headers=RESTRICTED)
    audit = api_client.get("/api/v2/audit?decision=deny", headers=ADMIN).json()
    assert audit["counts"].get("deny", 0) >= 1
    assert all(e["decision"] == "deny" for e in audit["events"])
    metrics = api_client.get("/api/v2/ops/metrics", headers=ADMIN).json()
    assert metrics["window"]["requests"] >= 1 and "rate_limit" in metrics
    assert api_client.get("/api/v2/patients", headers=ADMIN).status_code == 403
    assert api_client.get("/api/v2/ops/metrics", headers=CLINICIAN).status_code == 403


def test_feedback_is_recorded_without_side_effects(api_client) -> None:
    run = api_client.post("/api/v2/agent/runs", headers=CLINICIAN,
                          json={"patient_id": "synthetic_p001", "question": "HbA1c trend"}).json()
    response = api_client.post(f"/api/v2/agent/runs/{run['run_id']}/feedback", headers=CLINICIAN,
                               json={"category": "helpful", "rating": 5})
    assert response.json()["status"] == "recorded"


def test_request_size_and_rate_limits(api_client) -> None:
    big = api_client.post("/api/v2/agent/runs", headers={**CLINICIAN, "Content-Type": "application/json"},
                          content=b"{" + b" " * 40_000 + b"}")
    assert big.status_code == 413
    from api.middleware import RATE_LIMITER

    RATE_LIMITER.limits["agent"] = 2
    try:
        codes = [api_client.post("/api/v2/agent/runs", headers=CLINICIAN,
                                 json={"patient_id": "synthetic_p001", "question": "HbA1c"}).status_code
                 for _ in range(4)]
    finally:
        RATE_LIMITER.limits["agent"] = 20
    assert 429 in codes


def test_fhir_server_read_search_and_scope(api_client) -> None:
    meta = api_client.get("/fhir/r4/metadata").json()
    assert meta["fhirVersion"] == "4.0.1"
    page = api_client.get("/fhir/r4/Observation?patient=synthetic_p001&_count=5", headers=CLINICIAN).json()
    assert page["type"] == "searchset" and len(page["entry"]) == 5
    assert any(link["relation"] == "next" for link in page["link"])
    assert api_client.get("/fhir/r4/MedicationRequest/p004-m1/_history/1",
                          headers=CLINICIAN).json()["meta"]["versionId"] == "1"
    assert api_client.get("/fhir/r4/Patient/synthetic_p002", headers=RESTRICTED).status_code == 404
    assert api_client.get("/fhir/r4/Observation?patient=synthetic_p002", headers=RESTRICTED).status_code == 403


def test_v1_endpoints_still_work(api_client) -> None:
    assert api_client.get("/health").json()["status"] == "healthy"
    assert api_client.get("/").status_code == 200
