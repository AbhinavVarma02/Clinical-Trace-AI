"""M2 readiness (MOCK/offline): existing /api/v2 routes served from an Epic snapshot.

The snapshot is configured as a second data source (M2-S1, src/sources.py) and
selected per request with ``X-Data-Source``. Persona panels come from the snapshot
manifest; no persona is patched. The routes and schemas are the existing ones.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.middleware import RATE_LIMITER
from src.interop.epic import mapping, settings, workspace
from src.runtime import set_registry
from src.sources import SYNTHETIC_ORIGIN, build_registry
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
    registry = build_registry(
        "synthetic,epic-sandbox=m2-readiness-01", db_path=":memory:", provider=None, enable_model=False
    )
    set_registry(registry)
    RATE_LIMITER._buckets.clear()
    try:
        yield TestClient(app), snapshot.patient_ids[0]
    finally:
        set_registry(None)
        registry.close()


HEADERS = {"X-Demo-Persona": "clinician.demo", "X-Data-Source": "epic-sandbox"}


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


def test_status_origin_is_truthful_for_each_source(epic_api):
    client, _ = epic_api
    synthetic = client.get("/api/v2/system/status").json()
    assert synthetic["data"]["origin"] == SYNTHETIC_ORIGIN
    epic = client.get("/api/v2/system/status", headers={"X-Data-Source": "epic-sandbox"}).json()
    assert epic["data"]["origin"].startswith("Epic-shaped MOCK fixture") and "m2-readiness-01" in epic["data"]["origin"]
