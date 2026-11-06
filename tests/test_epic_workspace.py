"""Isolated Epic sandbox snapshots and bounded import (MOCK/offline: fixtures and mocked transport only)."""

from __future__ import annotations

import hashlib
import json

import pytest

from src.clinical.fhir_client import PatientScopeViolation
from src.interop.epic import importer, settings, workspace
from src.interop.epic.client import EpicPatientScopeError, EpicSandboxClient
from src.persistence.db import DEFAULT_DB_PATH
from tests.epic_support import RETRIEVED_AT, FakeTokens, fake_epic_transport, fixture_patient


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(settings, "REPO_ROOT", repo)
    monkeypatch.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    return settings.workspace_root()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _snapshot(snapshot_id: str = "test-snapshot-0001"):
    pid, resources, binaries = fixture_patient()
    return workspace.write_snapshot(
        pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id=snapshot_id
    )


def test_snapshot_is_isolated_labelled_and_excludes_medications_pending_a1(ws_root):
    before = _digest(DEFAULT_DB_PATH)
    snapshot = _snapshot()
    assert snapshot.path.parent == ws_root and snapshot.manifest["mode"] == "MOCK"
    assert snapshot.patient_ids == (snapshot.manifest["patients"][0]["internal_id"],)
    assert "MedicationRequest" not in snapshot.manifest["resource_counts"]
    assert "A1" in snapshot.manifest["mapping"]["skipped_types"]["MedicationRequest"]
    assert any("Readmission model disabled" in n for n in snapshot.manifest["notes"])
    assert snapshot.manifest["ingestion"]["invalid"] == 0
    assert _digest(DEFAULT_DB_PATH) == before  # the application's runtime database is untouched
    runtime = workspace.load_runtime(snapshot.path)
    try:
        assert runtime.store.patient_ids() == list(snapshot.patient_ids)
        assert runtime.model_available is False and runtime.provider is None
    finally:
        runtime.close()


def test_synthetic_runtime_still_has_exactly_the_four_synthetic_patients(ws_root):
    from src.runtime import build_runtime

    _snapshot()
    synthetic = build_runtime(":memory:", provider=None, enable_model=False)
    try:
        assert synthetic.store.patient_ids() == ["synthetic_p001", "synthetic_p002", "synthetic_p003", "synthetic_p004"]
    finally:
        synthetic.close()


def test_snapshots_are_immutable_and_tamper_evident(ws_root):
    snapshot = _snapshot()
    with pytest.raises(workspace.WorkspaceError, match="immutable"):
        _snapshot()
    bundle = snapshot.bundle_path
    bundle.write_bytes(bundle.read_bytes().replace(b"Fixture", b"Changed"))
    with pytest.raises(workspace.WorkspaceError, match="no longer matches"):
        workspace.open_snapshot(snapshot.path)


def test_verify_remaps_raw_data_and_detects_changed_raw_records(ws_root):
    snapshot = _snapshot()
    result = workspace.verify_snapshot(snapshot.path)
    assert result["ok"] and result["resources_checked"] == 11 and result["patients_isolated"]
    raw_file = snapshot.path / "raw" / "resources.json"
    raw = json.loads(raw_file.read_text(encoding="utf-8"))
    raw[1]["reasonCode"][0]["text"] = "edited after import"
    raw_file.write_text(json.dumps(raw), encoding="utf-8")
    tampered = workspace.verify_snapshot(snapshot.path)
    assert not tampered["ok"] and not tampered["bundle_matches_remap"]


def test_workspace_paths_are_confined_and_the_runtime_database_is_refused(ws_root, monkeypatch):
    for outside in (ws_root, ws_root.parent / "other", DEFAULT_DB_PATH, settings.REPO_ROOT / "var" / "x.db"):
        with pytest.raises(workspace.WorkspaceError):
            workspace.require_workspace_path(outside)
    sneaky = ws_root / "configured.db"
    monkeypatch.setenv("CLINICAL_TRACE_DB_PATH", str(sneaky))
    with pytest.raises(workspace.WorkspaceError, match="runtime database"):
        workspace.require_workspace_path(sneaky)


def test_cross_patient_or_unmappable_input_writes_nothing(ws_root):
    pid, resources, binaries = fixture_patient()
    _, bravo, _ = fixture_patient("bravo")
    with pytest.raises(PatientScopeViolation):
        workspace.write_snapshot(
            pid,
            [*resources, bravo[2]],
            binaries,
            retrieved_at=RETRIEVED_AT,
            mode="MOCK",
            snapshot_id="cross-patient-01",
        )
    without_patient = [r for r in resources if r["resourceType"] != "Patient"]
    with pytest.raises(workspace.WorkspaceError, match="Patient resource"):
        workspace.write_snapshot(
            pid, without_patient, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="no-patient-0001"
        )
    assert not ws_root.exists() or not any(ws_root.iterdir())


def test_medication_import_requires_the_approved_a1_fix(ws_root):
    pid, resources, binaries = fixture_patient()
    with pytest.raises(workspace.WorkspaceError, match="A1"):
        workspace.write_snapshot(
            pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", include_medications=True
        )


# --------------------------------------------------------------------------- bounded import (mocked sandbox)
def _client(transport):
    return EpicSandboxClient(FakeTokens(), transport=transport, sleep=lambda _s: None)  # type: ignore[arg-type]


def test_fetch_patient_reads_enabled_searches_and_only_cited_notes(ws_root):
    pid, resources, binaries = fixture_patient()
    seen = []
    with _client(fake_epic_transport(resources, binaries, requests=seen)) as client:
        fetched = importer.fetch_patient(client, pid, max_notes=10)
    ops = {op["operation"]: op for op in fetched.summary["operations"]}
    assert ops["MedicationRequest.Search"]["status"] == "skipped"
    assert all(op["status"] == "ok" for name, op in ops.items() if name != "MedicationRequest.Search")
    assert fetched.summary["complete"] is True and fetched.summary["notes"] == {"fetched": 1}
    assert set(fetched.binaries) == {"Binary/eFixtureBinNote1"}
    assert not any("MedicationRequest" in str(r.url) for r in seen)
    assert len(seen) == 1 + 7 + 1  # Patient.Read, 7 searches (medications skipped), 1 Binary.Read
    snapshot = workspace.write_snapshot(
        pid,
        fetched.raw_resources,
        fetched.binaries,
        retrieved_at=RETRIEVED_AT,
        mode="MOCK",
        fetch_summary=fetched.summary,
        snapshot_id="fetched-0001",
    )
    assert workspace.verify_snapshot(snapshot.path)["ok"]


def test_fetch_records_failed_operations_and_respects_the_note_cap(ws_root):
    pid, resources, binaries = fixture_patient()
    transport = fake_epic_transport(resources, binaries, failures={("DiagnosticReport", None): 403})
    with _client(transport) as client:
        fetched = importer.fetch_patient(client, pid, max_notes=0)
    failed = [op for op in fetched.summary["operations"] if op["status"] == "failed"]
    assert failed == [{"operation": "DiagnosticReport.Search", "status": "failed", "error": "forbidden"}]
    assert fetched.summary["complete"] is False
    assert fetched.summary["notes"] == {"skipped_note_cap": 1} and fetched.binaries == {}


def test_fetch_aborts_on_any_cross_patient_resource(ws_root):
    pid, resources, binaries = fixture_patient()
    _, bravo, _ = fixture_patient("bravo")
    leaked = {**bravo[2], "category": resources[6]["category"]}
    leaked["subject"] = {"reference": "Patient/eFixtureBravo0002-NotReal"}
    with _client(fake_epic_transport([*resources, leaked], binaries)) as client:
        with pytest.raises(EpicPatientScopeError):
            importer.fetch_patient(client, pid)


def test_smoke_reports_shapes_and_counts_without_values(ws_root):
    pid, resources, binaries = fixture_patient()
    with _client(fake_epic_transport(resources, binaries)) as client:
        result = importer.smoke(client, pid, include_all_types=True)
    assert result["all_ok"]
    rendered = json.dumps(result)
    for secret in ("Fixture Alpha", "pneumonia", "84", "eFixtureAlpha0001"):
        assert secret not in rendered
    labs = next(op for op in result["operations"] if op["operation"] == "Observation.Search (laboratory)")
    assert labs["count"] == 3 and labs["shape"]["meta.lastUpdated present"] == 0
