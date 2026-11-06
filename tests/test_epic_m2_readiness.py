"""M2 readiness (MOCK/offline, test-only): existing /api/v2 routes served from an Epic snapshot.

No production code is changed or configured here. The test swaps in the snapshot
runtime with ``set_runtime`` and temporarily widens two demo personas' panels with
``monkeypatch``. It shows the future local source switch (M2-S1, which needs separate
approval) would need no schema, route or UI change, and it records the one hard-coded
status string M2-S1 would have to make truthful.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.middleware import RATE_LIMITER
from src.interop.epic import mapping, settings, workspace
from src.runtime import set_runtime
from src.safety.policy import PERSONAS, Persona
from tests.epic_support import RETRIEVED_AT, fixture_patient


@pytest.fixture
def epic_api(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(settings, "REPO_ROOT", repo)
    monkeypatch.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    monkeypatch.delenv("CLINICAL_TRACE_PUBLIC_MODE", raising=False)
    monkeypatch.delenv("CLINICAL_TRACE_EDGE_SECRET", raising=False)
    pid, resources, binaries = fixture_patient()
    snapshot = workspace.write_snapshot(
        pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="m2-readiness-01"
    )
    runtime = workspace.load_runtime(snapshot.path)
    monkeypatch.setitem(
        PERSONAS, "clinician.demo", Persona("clinician.demo", "Demo Clinician", "clinician", snapshot.patient_ids)
    )
    monkeypatch.setitem(
        PERSONAS, "reviewer.demo", Persona("reviewer.demo", "Demo Reviewer", "reviewer", snapshot.patient_ids)
    )
    set_runtime(runtime)
    RATE_LIMITER._buckets.clear()
    try:
        yield TestClient(app), snapshot.patient_ids[0]
    finally:
        set_runtime(None)
        runtime.close()


HEADERS = {"X-Demo-Persona": "clinician.demo"}


def test_existing_patient_routes_serve_the_epic_snapshot(epic_api):
    client, pid = epic_api
    listing = client.get("/api/v2/patients", headers=HEADERS)
    assert listing.status_code == 200 and [p["patient_id"] for p in listing.json()] == [pid]
    detail = client.get(f"/api/v2/patients/{pid}", headers=HEADERS)
    assert detail.status_code == 200 and detail.json()["profile"]["synthetic"] is True
    timeline = client.get(f"/api/v2/patients/{pid}/timeline", headers=HEADERS)
    assert timeline.status_code == 200 and len(timeline.json()) >= 5


def test_existing_provenance_route_exposes_the_epic_lineage(epic_api):
    client, pid = epic_api
    encounter_id = mapping.internal_id("Encounter", "eFixtureEncInpatient1")
    response = client.get(f"/api/v2/patients/{pid}/resources/Encounter/{encounter_id}/provenance", headers=HEADERS)
    assert response.status_code == 200
    provenance = response.json()["provenance"]
    assert provenance["generator_source"] == f"{settings.FHIR_BASE}/Encounter/eFixtureEncInpatient1/_history/3"


def test_existing_agent_route_accepts_the_internal_epic_patient_id(epic_api):
    client, pid = epic_api
    response = client.post(
        "/api/v2/agent/runs",
        headers=HEADERS,
        json={"patient_id": pid, "question": "Summarize this patient's hospital course"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "answered" and body["inference"]["mode"] == "deterministic"
    assert body["verification"]["isolation_violations"] == 0


def test_status_origin_is_still_hard_coded_which_m2_s1_must_make_truthful(epic_api):
    client, _ = epic_api
    status = client.get("/api/v2/system/status").json()
    assert status["data"]["origin"] == "synthetic FHIR R4 fixture (no real patients)"
