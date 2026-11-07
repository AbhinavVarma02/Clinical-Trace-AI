"""Data-source registry (M2-S1): default behaviour, fail-closed configuration and source isolation.

Synthetic and MOCK Epic-shaped fixtures only. No network, no language model, no LIVE Epic snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.v2 as api_v2
from api.main import app
from api.middleware import RATE_LIMITER
from src.agent.llm import OpenAICompatibleProvider
from src.interop.epic import mapping, settings, workspace
from src.runtime import build_runtime, set_registry, set_runtime
from src.safety.policy import PERSONAS
from src.sources import (
    ENV_VAR,
    EPIC_PROVENANCE_URL,
    SYNTHETIC_ORIGIN,
    DataSourceError,
    SourceConfigError,
    SourceRegistry,
    SourceSpec,
    build_registry,
    parse_sources,
    source_provenance,
)
from tests.epic_support import RETRIEVED_AT, fixture_patient

REPO = Path(__file__).resolve().parents[1]
SYNTHETIC_IDS = ["synthetic_p001", "synthetic_p002", "synthetic_p003", "synthetic_p004"]
EPIC = "epic-sandbox"
QUESTION = "Summarize this patient's hospital course"


def headers(persona: str = "clinician.demo", source: str | None = None, **extra: str) -> dict[str, str]:
    result = {"X-Demo-Persona": persona, **extra}
    if source:
        result["X-Data-Source"] = source
    return result


def tree_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def epic_repo(tmp_path_factory):
    """Two MOCK snapshots (fixture patients alpha and bravo) in an isolated repository root."""
    repo = tmp_path_factory.mktemp("sources-repo")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "REPO_ROOT", repo)
        for name in ("CLINICAL_TRACE_DB_PATH", "CLINICAL_TRACE_PUBLIC_MODE", "CLINICAL_TRACE_EDGE_SECRET", ENV_VAR):
            mp.delenv(name, raising=False)
        snapshots = {}
        for name in ("alpha", "bravo"):
            pid, resources, binaries = fixture_patient(name)
            snapshots[name] = workspace.write_snapshot(
                pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id=f"sources-{name}-01"
            )
        yield snapshots


@pytest.fixture(scope="module")
def registry(epic_repo):
    built = build_registry(
        f"synthetic,{EPIC}={epic_repo['alpha'].manifest['snapshot_id']}",
        db_path=":memory:",
        provider=None,
        enable_model=False,
    )
    yield built
    built.close()


@pytest.fixture()
def client(registry):
    set_registry(registry)
    RATE_LIMITER._buckets.clear()
    try:
        yield TestClient(app)
    finally:
        set_registry(None)


@pytest.fixture()
def epic_pid(epic_repo) -> str:
    return epic_repo["alpha"].patient_ids[0]


# ------------------------------------------------------------------ configuration
@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_configuration_means_the_synthetic_fixture_only(value):
    assert parse_sources(value) == (SourceSpec("synthetic"),)


def test_configuration_lists_synthetic_first_then_epic_snapshots():
    assert parse_sources("synthetic, epic-sandbox = snap-0001 ,epic-sandbox-b=var/epic_sandbox/snap-0002") == (
        SourceSpec("synthetic"),
        SourceSpec("epic-sandbox", "snap-0001"),
        SourceSpec("epic-sandbox-b", "var/epic_sandbox/snap-0002"),
    )


@pytest.mark.parametrize(
    "value",
    [
        "epic-sandbox=snap-0001",  # synthetic must come first
        "synthetic,synthetic",
        "synthetic=fixture",
        "synthetic,epic-sandbox",
        "synthetic,epic-sandbox=",
        "synthetic,,epic-sandbox=snap-0001",
        "synthetic,other=snap-0001",
        "Synthetic",
        "synthetic,epic-sandbox-UPPER=snap-0001",
        "synthetic,epic-sandbox=a,epic-sandbox=b",
    ],
)
def test_invalid_configurations_are_refused(value):
    with pytest.raises(SourceConfigError):
        parse_sources(value)


def test_the_registry_requires_the_synthetic_source_first():
    with pytest.raises(SourceConfigError):
        SourceRegistry([])


def test_default_build_serves_only_the_synthetic_runtime(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    built = build_registry(db_path=":memory:", provider=None, enable_model=False)
    try:
        assert not built.multi and [s.source_id for s in built.sources()] == ["synthetic"]
        assert built.default.origin == SYNTHETIC_ORIGIN and built.default.personas is None
        assert built.get(None) is built.default and built.get("synthetic") is built.default
        for value in ("epic-sandbox", "Bad Value", "", "x" * 41):
            with pytest.raises(DataSourceError):
                built.get(value)
    finally:
        built.close()


def test_the_default_application_never_imports_the_epic_adapter(tmp_path):
    code = (
        "import sys\n"
        "from fastapi.testclient import TestClient\n"
        "from api.main import app\n"
        "with TestClient(app) as c:\n"
        "    r = c.get('/api/v2/patients', headers={'X-Demo-Persona': 'clinician.demo'})\n"
        "    assert r.status_code == 200 and len(r.json()) == 4, r.status_code\n"
        "print(sorted(m for m in sys.modules if m.startswith('src.interop')))\n"
    )
    env = {
        **os.environ,
        "CLINICAL_TRACE_SKIP_DOTENV": "1",
        "CLINICAL_TRACE_LLM_PROVIDER": "none",
        "LANGCHAIN_TRACING_V2": "false",
        "CLINICAL_TRACE_DB_PATH": str(tmp_path / "default.db"),
    }
    env.pop(ENV_VAR, None)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "[]"


def test_a_snapshot_outside_the_epic_workspace_is_refused(epic_repo, tmp_path):
    with pytest.raises(SourceConfigError, match="var/epic_sandbox"):
        build_registry(f"synthetic,{EPIC}={tmp_path}", db_path=":memory:", provider=None, enable_model=False)


def test_an_unknown_snapshot_is_refused(epic_repo):
    with pytest.raises(SourceConfigError, match="not an Epic sandbox snapshot"):
        build_registry(f"synthetic,{EPIC}=no-such-snapshot-01", db_path=":memory:", provider=None, enable_model=False)


def test_a_modified_snapshot_is_refused(epic_repo):
    pid, resources, binaries = fixture_patient("alpha")
    snapshot = workspace.write_snapshot(
        pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="sources-tampered-01"
    )
    with snapshot.bundle_path.open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(SourceConfigError, match="immutable"):
        build_registry(f"synthetic,{EPIC}=sources-tampered-01", db_path=":memory:", provider=None, enable_model=False)


def test_a_language_model_provider_refuses_epic_sources(epic_repo):
    alpha = epic_repo["alpha"].manifest["snapshot_id"]
    provider = OpenAICompatibleProvider("http://127.0.0.1:9/v1", "unit-test-model")
    with pytest.raises(SourceConfigError, match="deterministic-only"):
        build_registry(f"synthetic,{EPIC}={alpha}", db_path=":memory:", provider=provider, enable_model=False)
    # The synthetic-only configuration keeps its language-model mode, as before.
    synthetic_only = build_registry(
        "synthetic",
        db_path=":memory:",
        provider=OpenAICompatibleProvider("http://127.0.0.1:9/v1", "unit-test-model"),
        enable_model=False,
    )
    try:
        assert synthetic_only.default.runtime.provider is not None
    finally:
        synthetic_only.close()


def test_langsmith_tracing_refuses_epic_sources(epic_repo, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    alpha = epic_repo["alpha"].manifest["snapshot_id"]
    with pytest.raises(SourceConfigError, match="tracing"):
        build_registry(f"synthetic,{EPIC}={alpha}", db_path=":memory:", provider=None, enable_model=False)


def test_a_patient_may_appear_in_only_one_source(epic_repo):
    alpha = epic_repo["alpha"].manifest["snapshot_id"]
    with pytest.raises(SourceConfigError, match="appears in data sources"):
        build_registry(
            f"synthetic,{EPIC}={alpha},{EPIC}-2={alpha}", db_path=":memory:", provider=None, enable_model=False
        )


def test_two_epic_snapshots_are_served_as_separate_sources(epic_repo):
    alpha, bravo = epic_repo["alpha"], epic_repo["bravo"]
    built = build_registry(
        f"synthetic,{EPIC}={alpha.manifest['snapshot_id']},{EPIC}-bravo={bravo.manifest['snapshot_id']}",
        db_path=":memory:",
        provider=None,
        enable_model=False,
    )
    try:
        assert [s.source_id for s in built.sources()] == ["synthetic", EPIC, f"{EPIC}-bravo"]
        assert built.get(EPIC).runtime.store.patient_ids() == list(alpha.patient_ids)
        assert built.get(f"{EPIC}-bravo").runtime.store.patient_ids() == list(bravo.patient_ids)
        assert built.get(EPIC).persona("clinician.demo").panel == alpha.patient_ids
        assert built.get(f"{EPIC}-bravo").persona("clinician.demo").panel == bravo.patient_ids
        assert all(s.runtime.db is not built.default.runtime.db for s in built.sources()[1:])
    finally:
        built.close()


def test_diagnostic_availability_uses_only_the_selected_source_and_preserves_its_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    alpha_pid, alpha_resources, alpha_binaries = fixture_patient("alpha")
    bravo_pid, bravo_resources, bravo_binaries = fixture_patient("bravo")
    foreign = next(r for r in bravo_resources if r["resourceType"] == "Observation")
    raw_report = next(r for r in alpha_resources if r["resourceType"] == "DiagnosticReport")
    raw_report["result"].append({"reference": f"Observation/{foreign['id']}"})
    alpha = workspace.write_snapshot(
        alpha_pid,
        alpha_resources,
        alpha_binaries,
        retrieved_at=RETRIEVED_AT,
        mode="MOCK",
        snapshot_id="diagnostic-source-alpha",
    )
    bravo = workspace.write_snapshot(
        bravo_pid,
        bravo_resources,
        bravo_binaries,
        retrieved_at=RETRIEVED_AT,
        mode="MOCK",
        snapshot_id="diagnostic-source-bravo",
    )
    before = {"alpha": tree_hashes(alpha.path), "bravo": tree_hashes(bravo.path)}
    built = build_registry(
        f"synthetic,{EPIC}=diagnostic-source-alpha,{EPIC}-bravo=diagnostic-source-bravo",
        db_path=":memory:",
        provider=None,
        enable_model=False,
    )
    set_registry(built)
    RATE_LIMITER._buckets.clear()
    try:
        selected = built.get(EPIC).runtime
        foreign_id = mapping.internal_id("Observation", foreign["id"])
        assert selected.store.get("Observation", foreign_id) is None
        assert built.get(f"{EPIC}-bravo").runtime.store.get("Observation", foreign_id) is not None
        with TestClient(app) as c:
            pid = alpha.patient_ids[0]
            response = c.get(f"/api/v2/patients/{pid}", headers=headers(source=EPIC))
            assert response.status_code == 200
            (finding,) = [
                f for f in response.json()["missing_information"] if f["category"] == "diagnostic_result_unavailable"
            ]
            assert finding["severity"] == "medium"
            assert finding["unresolved_results"] == [f"Observation/{foreign_id}"]
            report_id = mapping.internal_id("DiagnosticReport", raw_report["id"])
            assert finding["source"]["reference"] == f"DiagnosticReport/{report_id}"
            lineage = c.get(
                f"/api/v2/patients/{pid}/resources/DiagnosticReport/{report_id}/provenance",
                headers=headers(source=EPIC),
            ).json()["source_provenance"]
            assert lineage["source_reference"] == f"DiagnosticReport/{raw_report['id']}"
            assert any("result[1].reference:internal-id" == field for field in lineage["field_status"]["transformed"])
            assert c.get(f"/api/v2/patients/{pid}", headers=headers(source=f"{EPIC}-bravo")).status_code == 403
            assert c.get(f"/api/v2/patients/{pid}", headers=headers()).status_code == 403
            for source, patient_id in ((None, "synthetic_p001"), (f"{EPIC}-bravo", bravo.patient_ids[0])):
                body = c.get(f"/api/v2/patients/{patient_id}", headers=headers(source=source)).json()
                assert not any(f["category"] == "diagnostic_result_unavailable" for f in body["missing_information"])
    finally:
        set_registry(None)
        built.close()
    assert {"alpha": tree_hashes(alpha.path), "bravo": tree_hashes(bravo.path)} == before


def test_an_invalid_configuration_stops_application_startup(monkeypatch):
    monkeypatch.setenv(ENV_VAR, f"{EPIC}=snapshot-first-01")
    set_registry(None)
    try:
        with pytest.raises(SourceConfigError):
            with TestClient(app):
                pass
    finally:
        set_registry(None)


def test_a_valid_configuration_is_built_at_startup(epic_repo, monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, f"synthetic,{EPIC}={epic_repo['alpha'].manifest['snapshot_id']}")
    monkeypatch.setenv("CLINICAL_TRACE_DB_PATH", str(tmp_path / "startup.db"))
    set_registry(None)
    RATE_LIMITER._buckets.clear()
    try:
        with TestClient(app) as started:
            status = started.get("/api/v2/system/status", headers={"X-Data-Source": EPIC}).json()
            assert status["data"]["source"] == EPIC and status["data"]["patients"] == 1
    finally:
        set_registry(None)


# ------------------------------------------------------------------ default mode
def test_default_status_names_no_sources(api_client):
    plain = api_client.get("/api/v2/system/status").json()
    assert "source" not in plain["data"] and "sources" not in plain["data"]
    assert plain["data"]["origin"] == SYNTHETIC_ORIGIN
    named = api_client.get("/api/v2/system/status", headers={"X-Data-Source": "synthetic"}).json()
    plain.pop("uptime_s"), named.pop("uptime_s")
    assert named == plain


@pytest.mark.parametrize("value", [EPIC, "Epic Sandbox", "unknown-source"])
def test_default_mode_refuses_any_other_source(api_client, value):
    for path in ("/api/v2/system/status", "/api/v2/personas", "/api/v2/patients", "/api/v2/patients/synthetic_p001"):
        response = api_client.get(path, headers=headers(source=value))
        assert response.status_code == 400, path
        error = response.json()["error"]
        assert error["code"] == "invalid_data_source" and value not in error["message"]


def test_personas_and_policy_matrix_need_no_runtime_without_a_source(api_client, monkeypatch):
    def unavailable():
        raise AssertionError("the data-source registry was requested")

    monkeypatch.setattr(api_v2, "get_registry", unavailable)
    for path in ("/api/v2/personas", "/api/v2/policy/matrix", "/api/v2/tools"):
        assert api_client.get(path).status_code == 200, path


def test_default_personas_and_policy_matrix_are_the_synthetic_ones(api_client):
    listed = {p["persona_id"]: p["panel"] for p in api_client.get("/api/v2/personas").json()}
    assert listed == {pid: list(p.panel) for pid, p in PERSONAS.items()}
    assert api_client.get("/api/v2/policy/matrix").json()["patients"] == SYNTHETIC_IDS


# ------------------------------------------------------------------ two sources
def test_status_describes_each_source_truthfully(client, epic_repo):
    plain = client.get("/api/v2/system/status").json()
    assert plain["data"]["source"] == "synthetic" and plain["data"]["origin"] == SYNTHETIC_ORIGIN
    synthetic, epic = plain["data"]["sources"]
    assert synthetic == {
        "id": "synthetic",
        "kind": "synthetic-fixture",
        "origin": SYNTHETIC_ORIGIN,
        "patients": 4,
        "inference": "deterministic",
        "readmission_model": False,
        "default": True,
    }
    assert epic["id"] == EPIC and epic["kind"] == "epic-sandbox-snapshot" and epic["default"] is False
    assert epic["patients"] == 1 and epic["inference"] == "deterministic" and epic["readmission_model"] is False
    assert epic["snapshot"] == {
        "snapshot_id": "sources-alpha-01",
        "mode": "MOCK",
        "retrieved_at": RETRIEVED_AT,
        "source": epic_repo["alpha"].manifest["source"],
        "mapping_version": epic_repo["alpha"].manifest["mapping_version"],
    }
    selected = client.get("/api/v2/system/status", headers={"X-Data-Source": EPIC}).json()
    assert selected["data"]["source"] == EPIC and selected["data"]["patients"] == 1
    assert (
        selected["data"]["origin"].startswith("Epic-shaped MOCK fixture")
        and "not Epic data" in selected["data"]["origin"]
    )
    assert selected["readmission_model"]["loaded"] is False and selected["evaluation"] is None
    assert selected["inference"]["mode"] == "deterministic"


def test_each_source_lists_only_its_own_patients(client, epic_pid):
    synthetic = [p["patient_id"] for p in client.get("/api/v2/patients", headers=headers()).json()]
    epic = [p["patient_id"] for p in client.get("/api/v2/patients", headers=headers(source=EPIC)).json()]
    assert synthetic == SYNTHETIC_IDS and epic == [epic_pid]
    assert client.get("/api/v2/patients", headers=headers("clinician.restricted", EPIC)).json() == []
    assert client.get("/api/v2/patients", headers=headers("admin.demo", EPIC)).status_code == 403


def test_a_patient_is_unreachable_through_the_other_source(client, epic_pid):
    for source, pid in ((None, epic_pid), (EPIC, "synthetic_p001")):
        h = headers(source=source)
        for path in ("", "/timeline", "/risk", "/coverage", f"/resources/Patient/{pid}/provenance"):
            assert client.get(f"/api/v2/patients/{pid}{path}", headers=h).status_code == 403, (source, path)
        assert (
            client.post(f"/api/v2/patients/{pid}/retrieve", headers=h, json={"query": "discharge"}).status_code == 403
        )
        assert (
            client.post("/api/v2/agent/runs", headers=h, json={"patient_id": pid, "question": QUESTION}).status_code
            == 403
        )
        # The FHIR read route answers 404 for a resource it may not show or does not hold.
        assert client.get(f"/fhir/r4/Patient/{pid}", headers=h).status_code == 404


def test_runs_reviews_and_audit_stay_in_their_source(client, epic_pid):
    epic = headers(source=EPIC)
    run = client.post("/api/v2/agent/runs", headers=epic, json={"patient_id": epic_pid, "question": QUESTION})
    assert run.status_code == 200
    body = run.json()
    assert body["outcome"] == "answered" and body["inference"]["mode"] == "deterministic"
    assert body["verification"]["isolation_violations"] == 0
    run_id = body["run_id"]
    assert client.get(f"/api/v2/agent/runs/{run_id}", headers=headers()).status_code == 404
    assert client.get(f"/api/v2/agent/runs/{run_id}/status", headers=headers()).status_code == 404
    assert client.post("/api/v2/reviews", headers=headers(), json={"run_id": run_id}).status_code == 404
    assert client.get(f"/api/v2/agent/runs/{run_id}", headers=epic).status_code == 200
    review = client.post("/api/v2/reviews", headers=epic, json={"run_id": run_id})
    assert review.status_code == 200
    review_id, artifact_hash = review.json()["review_id"], review.json()["artifact_hash"]
    assert review_id not in [r["review_id"] for r in client.get("/api/v2/reviews", headers=headers()).json()]
    assert client.get(f"/api/v2/reviews/{review_id}", headers=headers()).status_code == 404
    decided = client.post(
        f"/api/v2/reviews/{review_id}/decision",
        headers=headers("reviewer.demo", EPIC),
        json={"decision": "approved", "artifact_hash": artifact_hash},
    )
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    assert run_id not in [r["run_id"] for r in client.get("/api/v2/agent/recent", headers=headers()).json()]
    assert run_id in [r["run_id"] for r in client.get("/api/v2/agent/recent", headers=epic).json()]
    synthetic_audit = client.get("/api/v2/audit?limit=500", headers=headers("admin.demo")).json()["events"]
    epic_audit = client.get("/api/v2/audit?limit=500", headers=headers("admin.demo", EPIC)).json()["events"]
    # Denied attempts through the synthetic source are audited there; access is allowed only in the Epic source.
    assert not any(e["resource"] == f"Patient/{epic_pid}" and e["decision"] == "allow" for e in synthetic_audit)
    assert any(e["resource"] == f"Patient/{epic_pid}" and e["decision"] == "allow" for e in epic_audit)
    assert client.get(f"/fhir/r4/Patient/{epic_pid}", headers=epic).status_code == 200
    other = client.post(
        "/api/v2/agent/runs",
        headers=headers(),
        json={"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"},
    ).json()
    assert client.get(f"/api/v2/agent/runs/{other['run_id']}", headers=epic).status_code == 404


def test_workspaces_stay_isolated_inside_an_epic_source(client, epic_pid, monkeypatch):
    monkeypatch.setenv("CLINICAL_TRACE_PUBLIC_MODE", "1")
    missing = client.get("/api/v2/patients", headers=headers(source=EPIC))
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "workspace_required"
    a = headers(source=EPIC, **{"X-Demo-Workspace": "workspace-aaaaaaaaaaaaaaaa"})
    b = headers(source=EPIC, **{"X-Demo-Workspace": "workspace-bbbbbbbbbbbbbbbb"})
    run_id = client.post("/api/v2/agent/runs", headers=a, json={"patient_id": epic_pid, "question": QUESTION}).json()[
        "run_id"
    ]
    assert client.get(f"/api/v2/agent/runs/{run_id}", headers=b).status_code == 404
    assert client.get(f"/api/v2/agent/runs/{run_id}", headers=a).status_code == 200
    client.post("/api/v2/reviews", headers=a, json={"run_id": run_id})
    assert client.get("/api/v2/reviews", headers=b).json() == []


def test_requests_are_logged_in_the_source_that_served_them(client, registry, epic_pid):
    def count(source) -> int:
        return source.runtime.db.one("SELECT COUNT(*) AS n FROM request_log")["n"]

    synthetic, epic = registry.default, registry.get(EPIC)
    before = count(synthetic), count(epic)
    for _ in range(3):
        assert client.get(f"/api/v2/patients/{epic_pid}/timeline", headers=headers(source=EPIC)).status_code == 200
    assert count(synthetic) == before[0] and count(epic) == before[1] + 3
    client.get("/api/v2/patients", headers=headers())
    assert count(synthetic) == before[0] + 1


def test_provenance_carries_the_epic_lineage_and_synthetic_provenance_is_unchanged(client, epic_pid):
    encounter = mapping.internal_id("Encounter", "eFixtureEncInpatient1")
    body = client.get(
        f"/api/v2/patients/{epic_pid}/resources/Encounter/{encounter}/provenance", headers=headers(source=EPIC)
    ).json()
    lineage = body["source_provenance"]
    assert body["provenance"]["generator_source"] == lineage["source_url"]
    assert lineage["source_url"].startswith(f"{settings.FHIR_BASE}/Encounter/eFixtureEncInpatient1")
    assert lineage["source_reference"] == "Encounter/eFixtureEncInpatient1"
    assert lineage["retrieved_at"] == RETRIEVED_AT and lineage["snapshot_id"] == "sources-alpha-01"
    assert "never a clinical time" in lineage["retrieved_at_note"]
    assert set(lineage["field_status"]) == {"transformed", "substituted", "unavailable", "unsupported"}
    assert lineage["field_status"]["transformed"]
    synthetic = client.get(
        "/api/v2/patients/synthetic_p001/resources/Patient/synthetic_p001/provenance", headers=headers()
    ).json()
    assert set(synthetic) == {"provenance", "history"}


def test_coverage_is_declared_for_epic_and_holds_no_record_content(client, epic_pid, epic_repo):
    response = client.get(f"/api/v2/patients/{epic_pid}/coverage", headers=headers(source=EPIC))
    assert response.status_code == 200
    body = response.json()
    manifest = epic_repo["alpha"].manifest
    assert body["declared"] is True and body["source"] == EPIC and body["kind"] == "epic-sandbox-snapshot"
    assert body["structured_data"] == {"MedicationRequest": "not_imported"}
    assert body["snapshot"]["mode"] == "MOCK" and body["resource_counts"] == manifest["resource_counts"]
    assert (
        body["scope_notes"] == manifest["notes"]
        and body["mapping"]["field_status"] == manifest["mapping"]["field_status"]
    )
    text = json.dumps(body)
    source_patient_id, _, binaries = fixture_patient("alpha")
    assert "source_patient_id" not in text and source_patient_id not in text
    for binary in binaries.values():
        note = binary.data.decode("utf-8")
        assert note[len(note) // 3 : len(note) // 3 + 40] not in text
    synthetic = client.get("/api/v2/patients/synthetic_p001/coverage", headers=headers()).json()
    assert synthetic["declared"] is False and synthetic["source"] == "synthetic"


def test_the_offline_evaluation_is_reported_for_the_synthetic_fixture_only(client):
    epic = client.get("/api/v2/evaluation/latest", headers=headers(source=EPIC))
    assert epic.status_code == 404 and "synthetic fixture only" in epic.json()["error"]["message"]
    synthetic = client.get("/api/v2/evaluation/latest", headers=headers())
    assert "synthetic fixture only" not in synthetic.text


def test_epic_personas_and_policy_matrix_come_from_the_snapshot(client, epic_pid):
    epic = {p["persona_id"]: p["panel"] for p in client.get("/api/v2/personas", headers={"X-Data-Source": EPIC}).json()}
    assert epic == {
        "clinician.demo": [epic_pid],
        "clinician.restricted": [],
        "reviewer.demo": [epic_pid],
        "admin.demo": [],
    }
    synthetic = {p["persona_id"]: p["panel"] for p in client.get("/api/v2/personas").json()}
    assert synthetic == {pid: list(p.panel) for pid, p in PERSONAS.items()}
    matrix = client.get("/api/v2/policy/matrix", headers={"X-Data-Source": EPIC}).json()
    assert matrix["patients"] == [epic_pid]
    restricted = [r for r in matrix["rows"] if r["persona"] == "clinician.restricted" and r["patient_scoped"]]
    assert restricted and not any(r["decisions"][epic_pid] for r in restricted)


def test_an_added_source_does_not_change_synthetic_answers(client):
    request = {"patient_id": "synthetic_p001", "question": "Why was metformin stopped?"}
    with_epic = client.post("/api/v2/agent/runs", headers=headers(), json=request).json()
    alone = build_runtime(":memory:", provider=None, enable_model=False)
    set_runtime(alone)
    try:
        single = TestClient(app).post("/api/v2/agent/runs", headers=headers(), json=request).json()
    finally:
        set_runtime(None)
        alone.close()
    for key in ("outcome", "answer", "verification", "evidence"):
        assert with_epic[key] == single[key], key


def test_serving_a_snapshot_never_writes_into_it(epic_repo, epic_pid):
    directory = epic_repo["alpha"].path
    before = tree_hashes(directory)
    built = build_registry(f"synthetic,{EPIC}=sources-alpha-01", db_path=":memory:", provider=None, enable_model=False)
    set_registry(built)
    RATE_LIMITER._buckets.clear()
    try:
        api = TestClient(app)
        run_id = api.post(
            "/api/v2/agent/runs", headers=headers(source=EPIC), json={"patient_id": epic_pid, "question": QUESTION}
        ).json()["run_id"]
        assert api.post("/api/v2/reviews", headers=headers(source=EPIC), json={"run_id": run_id}).status_code == 200
    finally:
        set_registry(None)
        built.close()
    assert tree_hashes(directory) == before


def test_the_provenance_extension_url_matches_the_adapter():
    assert EPIC_PROVENANCE_URL == mapping.PROVENANCE_URL
    assert source_provenance({"resourceType": "Patient", "id": "synthetic_p001"}) is None
