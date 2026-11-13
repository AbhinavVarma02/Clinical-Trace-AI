"""Offline-only LIVE record contract. Every byte is invented by test fixtures, never read from Epic or private data.

Reuse the existing test-only FixtureLive adapter and real staging/approval/integrity path. The corruption helper
below rewrites only this test's synthetic generation, including hashes, to test semantic isolation beyond hashing.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections import Counter
from inspect import signature
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api import beta_live_record as projection
from api.beta import create_mock_beta_app
from src.interop.epic import settings
from src.interop.epic.beta_catalog import MOCK_PATIENTS, RefreshRejected
from src.interop.epic.beta_generations import CONTINUED_S, FRESH_S
from src.interop.epic.beta_live_acquisition import LiveRefresh
from src.interop.epic.beta_live_generations import GENERATION_RETENTION_S
from src.interop.epic.beta_live_staging import LiveAllowlist
from src.interop.epic.beta_staging import encoded
from src.interop.epic.mapping import content_sha256, internal_id
from tests.test_beta_browser_bootstrap import READ, assets as assets_fixture
from tests.test_beta_generations import BODY
from tests.test_beta_live_generations import World, keys as keys_fixture, world as world_fixture
from tests.test_beta_live_route import LIVE_ALIAS, LIVE_PATIENT, FakeEpic
from tests.test_beta_owner_refresh import ORIGIN, harness as harness_fixture

assets = assets_fixture
keys = keys_fixture
world = world_fixture
harness = harness_fixture

SECTIONS = ("conditions", "encounters", "labs", "vitals", "reports", "notes")


def approved(world, label="Synthetic LIVE fixture Alpha"):
    world.epic.records[0]["name"] = [{"text": label}]
    world.epic.outcomes = {"DocumentReference:clinical-note": "59100"}
    attempt, digest = world.candidate()
    return world.approve(attempt, digest)["generation_id"]


def read(world, alias=LIVE_ALIAS, authorized=lambda: True):
    return projection.live_record_view(world.gens, alias, authorized=authorized)


def refused(world, code, status=409, **kwargs):
    with pytest.raises(RefreshRejected) as error:
        read(world, **kwargs)
    assert (error.value.status, error.value.code) == (status, code)


def test_live_projection_never_reports_mapper_fallback_as_upstream_timestamp_or_version(world):
    world.epic.records[0].pop("meta", None)
    approved(world)
    source = read(world)["record"]["patient"]["source"]
    assert source["version"] is None and source["last_updated"] is None
    assert source["content_sha256"] == content_sha256(world.epic.records[0])


def rewrite_synthetic(world, mutate):
    """TEST ONLY: corrupt this pytest fixture's approved evidence while keeping its byte hashes consistent."""
    gid = world.head()["active"]
    directory = world.gens.root / "objects" / gid
    manifest = json.loads((directory / "manifest.json").read_bytes())
    bundle = json.loads((directory / "normalized_bundle.json").read_bytes())
    mutate(manifest, bundle)
    bundle_bytes = encoded(bundle)
    manifest["bundle_sha256"] = content_sha256(bundle_bytes)
    manifest_bytes = encoded(manifest)
    (directory / "normalized_bundle.json").write_bytes(bundle_bytes)
    (directory / "manifest.json").write_bytes(manifest_bytes)
    with world.catalog.db:
        world.catalog.db.execute(
            "UPDATE live_generation SET manifest_sha256 = ? WHERE generation_id = ?",
            (content_sha256(manifest_bytes), gid),
        )


def test_verified_live_shaped_generation_projects_all_record_sections_and_exact_provenance(world):
    gid = approved(world)
    view = read(world)
    assert (view["mode"], view["source"], view["data_origin"]) == (
        "LIVE",
        settings.SOURCE_SYSTEM,
        "synthetic_live_fixture",
    )
    assert (view["serving"], view["publication"], view["freshness"]) == ("unavailable", "disabled", "fresh")
    assert view["completeness"] == "unconfirmed" and view["active"]["generation_id"] == gid
    record = view["record"]
    assert record["patient"]["display_name"] == "Synthetic LIVE fixture Alpha"
    assert {section: record[section]["total"] for section in SECTIONS} == dict.fromkeys(SECTIONS, 1) | {"conditions": 2}
    assert {item["category"] for item in record["conditions"]["items"]} == {"problem-list-item", "encounter-diagnosis"}
    encounter = record["encounters"]["items"][0]
    assert (encounter["class"], encounter["status"], encounter["length_of_stay_days"]) == ("AMB", "finished", 0)
    lab, vital = record["labs"]["items"][0], record["vitals"]["items"][0]
    assert (lab["name"], lab["value"], lab["unit"]) == ("Creatinine", 1.2, "mg/dL")
    assert (vital["name"], vital["value"], vital["unit"]) == ("Heart rate", 70, "/min")
    report = record["reports"]["items"][0]
    assert (report["name"], report["conclusion"], report["result_count"]) == (
        "Cardiac stress test",
        "Stress test showed no ischemia.",
        2,
    )
    note = record["notes"]["items"][0]
    assert (note["type"], note["category"], note["status"]) == ("Clinical note", "clinical-note", "current")
    assert note["source"]["reference"] == f"DocumentReference/{internal_id('DocumentReference', 'metadata')}"
    assert note["source"]["version"] == "upstream-7"  # retained source metadata, not an invented version
    assert all(item["source"]["reference"] for section in SECTIONS for item in record[section]["items"])
    manifest_path = world.gens.root / "objects" / gid / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    assert view["provenance"]["manifest_sha256"] == content_sha256(manifest_bytes)
    assert view["provenance"]["bundle_sha256"] == manifest["bundle_sha256"]
    assert view["provenance"]["resource_counts"] == manifest["resource_counts"]
    assert view["provenance"]["synthetic_transport"] is True
    coverage = view["coverage"]
    assert coverage["completeness"] == "unconfirmed" and coverage["unresolved_references"] == 1
    assert coverage["note_bodies"] == "not_imported"
    assert coverage["sources"]["MedicationRequest"] == "not_imported"
    assert coverage["sources"]["DocumentReference.metadata"] == "returned"
    assert coverage["operations"]["DocumentReference:clinical-note"]["informational_codes"] == ["59100"]
    assert coverage["operations"]["DocumentReference:clinical-note"]["import"] == "not_imported"
    assert coverage["operations"]["MedicationRequest"]["execution"] == "not_requested"
    codes = {warning["code"] for warning in view["warnings"]}
    assert {
        "synthetic_live_fixture",
        "live_access_disabled",
        "completeness_unconfirmed",
        "upstream_information",
        "note_bodies_not_imported",
        "medications_not_imported",
        "unresolved_references",
    } <= codes
    assert "synthetic_mock" not in codes and world.gens.real_promotion is False
    text = json.dumps(view)
    for forbidden in (BODY, "Binary/secret", LIVE_PATIENT, "never-read.key", "invented-live-route-token"):
        assert forbidden not in text


def test_projection_uses_only_a_closed_memory_database_and_does_not_change_generation_or_catalog(world, monkeypatch):
    approved(world)
    before_files = {str(path): path.read_bytes() for path in world.gens.root.rglob("*") if path.is_file()}
    before_catalog = list(world.catalog.db.iterdump())
    databases = []
    database_type = projection.Database

    def memory_only(path):
        assert path == ":memory:"
        database = database_type(path)
        databases.append(database)
        return database

    monkeypatch.setattr(projection, "Database", memory_only)
    monkeypatch.setenv("CLINICAL_TRACE_DB_PATH", str(world.root / "must-not-exist.sqlite"))
    assert read(world)["record"] is not None
    assert len(databases) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        databases[0].one("SELECT 1")
    assert list(world.catalog.db.iterdump()) == before_catalog
    assert before_files == {str(path): path.read_bytes() for path in world.gens.root.rglob("*") if path.is_file()}
    assert not (world.root / "must-not-exist.sqlite").exists() and world.temporary() == []


def test_memory_store_is_closed_even_if_projection_fails(world, monkeypatch):
    approved(world)
    databases = []
    database_type = projection.Database

    def track(path):
        database = database_type(path)
        databases.append(database)
        return database

    def fail(*args):
        raise ValueError("synthetic internal detail must not escape")

    monkeypatch.setattr(projection, "Database", track)
    monkeypatch.setattr(projection, "_record", fail)
    refused(world, "generation_integrity")
    with pytest.raises(sqlite3.ProgrammingError):
        databases[0].one("SELECT 1")


def test_zero_values_missing_units_and_empty_sections_are_not_fabricated(world):
    world.epic.records = [
        resource
        for resource in world.epic.records
        if resource["resourceType"] not in {"Condition", "DiagnosticReport", "DocumentReference"}
    ]
    lab = next(resource for resource in world.epic.records if resource.get("id") == "laboratory")
    lab["valueQuantity"] = {"value": 0}
    approved(world)
    view = read(world)
    lab = view["record"]["labs"]["items"][0]
    assert lab["value"] == 0 and lab["unit"] is None and lab["unit_missing"] is True
    for section in ("conditions", "reports", "notes"):
        assert view["record"][section] == {"items": [], "total": 0, "truncated": False}
    assert view["coverage"]["operations"]["Condition:problem-list-item"]["import"] == "returned_none"
    assert view["coverage"]["sources"]["DocumentReference.metadata"] == "returned_none"
    gid = view["active"]["generation_id"]
    manifest = json.loads((world.gens.root / "objects" / gid / "manifest.json").read_bytes())
    assert view["coverage"]["field_status"] == manifest["coverage"]["mapping"]["field_status"]
    assert "completeness_unconfirmed" in {item["code"] for item in view["warnings"]}


def test_section_limit_is_explicit_without_changing_coverage(world, monkeypatch):
    approved(world)
    import api.beta_record as shared

    monkeypatch.setattr(shared, "LIMIT", 1)
    view = read(world)
    assert view["record"]["conditions"]["total"] == 2
    assert len(view["record"]["conditions"]["items"]) == 1 and view["record"]["conditions"]["truncated"] is True
    assert view["provenance"]["resource_counts"]["Condition"] == 2


@pytest.mark.parametrize("age,freshness", [(FRESH_S + 1, "stale"), (1, "cached")])
def test_freshness_and_failed_refresh_are_reported_without_enabling_serving(world, age, freshness):
    approved(world)
    world.clock.now += age
    attempt, _ = world.candidate()  # newer staged candidate does not replace the approved generation
    with world.catalog.db:
        world.catalog.db.execute(
            "UPDATE attempt SET state = 'failed', error_code = 'deadline' WHERE attempt_id = ?", (attempt,)
        )
    view = read(world)
    assert view["freshness"] == freshness and view["serving"] == "unavailable"
    assert view["record"] is not None
    assert {f"serving_{freshness}", "last_attempt_failed"} <= {item["code"] for item in view["warnings"]}


@pytest.mark.parametrize("alias", ["mock-alpha", "approved-bravo", "../approved-alpha"])
def test_an_alias_cannot_read_another_patient_or_the_mock_tier(world, alias):
    approved(world)
    refused(world, "patient_unavailable", 404, alias=alias)


def test_missing_or_only_staged_generations_have_no_record_projection(world):
    refused(world, "generation_unavailable", 404)
    world.candidate()
    refused(world, "generation_unavailable", 404)


@pytest.mark.parametrize(
    "change,status,code",
    [
        ("unauthorized", 401, "owner_required"),
        ("owner_revoked", 404, "patient_unavailable"),
        ("patient_revoked", 404, "patient_unavailable"),
        ("inactive", 404, "generation_unavailable"),
        ("erase_pending", 404, "generation_unavailable"),
        ("expired", 410, "generation_expired"),
        ("too_old", 409, "generation_unavailable"),
    ],
)
def test_authorization_active_pointer_retention_and_erasure_are_required(world, change, status, code):
    approved(world)
    if change == "owner_revoked":
        world.catalog.revoke("owner")
    elif change == "patient_revoked":
        world.catalog.revoke(LIVE_ALIAS)
    elif change == "inactive":
        world.catalog.db.execute("UPDATE patient_head SET active = NULL WHERE alias = ?", (LIVE_ALIAS,))
    elif change == "erase_pending":
        world.catalog.db.execute("UPDATE live_generation SET state = 'erase_pending'")
    elif change == "expired":
        world.clock.now += GENERATION_RETENTION_S
    elif change == "too_old":
        world.clock.now += CONTINUED_S + 1
    refused(world, code, status, authorized=lambda: change != "unauthorized")


@pytest.mark.parametrize("change", ["authorization", "owner_revoked", "patient_revoked", "pointer", "expiry"])
def test_projection_rechecks_authorization_pointer_and_expiry_before_returning(world, monkeypatch, change):
    approved(world)
    record = projection._record
    allowed = True

    def interleave(*args):
        nonlocal allowed
        result = record(*args)
        if change == "authorization":
            allowed = False
        elif change == "owner_revoked":
            world.catalog.revoke("owner")
        elif change == "patient_revoked":
            world.catalog.revoke(LIVE_ALIAS)
        elif change == "pointer":
            world.catalog.db.execute("UPDATE patient_head SET active = NULL WHERE alias = ?", (LIVE_ALIAS,))
        else:
            world.clock.now += GENERATION_RETENTION_S
        return result

    monkeypatch.setattr(projection, "_record", interleave)
    code, status = {
        "authorization": ("owner_required", 401),
        "owner_revoked": ("patient_unavailable", 404),
        "patient_revoked": ("patient_unavailable", 404),
        "pointer": ("publication_fenced", 409),
        "expiry": ("generation_expired", 410),
    }[change]
    refused(world, code, status, authorized=lambda: allowed)


@pytest.mark.parametrize("file", ["manifest.json", "normalized_bundle.json"])
def test_tampered_evidence_is_rejected_before_any_projection(world, monkeypatch, file):
    gid = approved(world)
    path = world.gens.root / "objects" / gid / file
    path.write_bytes(path.read_bytes() + b" ")

    def no_projection(*args):
        pytest.fail("unverified bytes reached the record projection")

    monkeypatch.setattr(projection, "_record", no_projection)
    refused(world, "generation_integrity")


def test_unverified_storage_is_refused_before_reading_evidence(world, monkeypatch):
    from src.interop.epic import beta_live_staging as files
    from src.interop.epic.beta_acquisition import AcquisitionError

    approved(world)

    def unverified(path):
        raise AcquisitionError("acl_unverified")

    def never_read(*args):
        pytest.fail("unverified storage was read")

    monkeypatch.setattr(files, "verify_private_path", unverified)
    monkeypatch.setattr(world.gens, "_read_evidence", never_read)
    refused(world, "acl_unverified", 503)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "MOCK"),
        ("source", "epic-sandbox-mock"),
        ("policy", "mock-initial-r4-v1"),
        ("alias", "approved-bravo"),
        ("patient_id", "synthetic_other"),
        ("generation_id", "0" * 32),
        ("attempt_id", "different-attempt"),
        ("completeness", "complete"),
        ("mapping_version", "different-version"),
        ("retrieved_at", 1),
    ],
)
def test_even_hash_consistent_evidence_must_match_the_live_tier_and_catalog(world, field, value):
    approved(world)
    rewrite_synthetic(world, lambda manifest, bundle: manifest.update({field: value}))
    refused(world, "generation_integrity")


@pytest.mark.parametrize(
    "change",
    ["subject", "nested_patient", "extra_patient", "duplicate", "wrong_count", "document_patient", "medication"],
)
def test_hashes_alone_cannot_admit_mixed_patient_or_out_of_policy_resources(world, change):
    approved(world)

    def corrupt(manifest, bundle):
        resource = next(
            entry["resource"] for entry in bundle["entry"] if entry["resource"]["resourceType"] == "Condition"
        )
        if change == "subject":
            resource["subject"]["reference"] = "Patient/synthetic_other"
        elif change == "nested_patient":
            resource["encounter"] = {"reference": "Patient/synthetic_other"}
        elif change == "extra_patient":
            other = copy.deepcopy(bundle["entry"][-1])
            other["resource"] = copy.deepcopy(
                next(entry["resource"] for entry in bundle["entry"] if entry["resource"]["resourceType"] == "Patient")
            )
            other["resource"]["id"] = "synthetic_other"
            bundle["entry"].append(other)
        elif change == "duplicate":
            bundle["entry"].append({"resource": copy.deepcopy(resource)})
        elif change == "wrong_count":
            manifest["resource_counts"]["Condition"] += 1
            return
        elif change == "document_patient":
            manifest["document_metadata"][0]["subject"]["reference"] = "Patient/invented-other-patient"
        else:
            resource["resourceType"] = "MedicationRequest"
        manifest["resource_counts"] = dict(Counter(entry["resource"]["resourceType"] for entry in bundle["entry"]))

    rewrite_synthetic(world, corrupt)
    refused(world, "generation_integrity")


def test_synthetic_label_cannot_be_removed_to_read_real_evidence_with_promotion_disabled(world):
    approved(world)
    rewrite_synthetic(world, lambda manifest, bundle: manifest.update(synthetic_transport=False))
    refused(world, "real_promotion_disabled")
    assert world.gens.real_promotion is False


def test_stores_with_reused_resource_identifiers_cannot_share_projected_records(world, tmp_path):
    approved(world)
    other_root = tmp_path / "other-owner-store"
    other_root.mkdir(mode=0o700)
    other = World(other_root, world.key, FakeEpic())
    try:
        approved(other, "Synthetic independent fixture Bravo")
        alpha, bravo = read(world), read(other)
        assert alpha["record"]["patient"]["display_name"] == "Synthetic LIVE fixture Alpha"
        assert bravo["record"]["patient"]["display_name"] == "Synthetic independent fixture Bravo"
        assert alpha["active"]["generation_id"] != bravo["active"]["generation_id"]
        assert "independent fixture Bravo" not in json.dumps(alpha)
        world.catalog.revoke(LIVE_ALIAS)
        refused(world, "patient_unavailable", 404)
        assert read(other)["record"]["patient"]["display_name"] == "Synthetic independent fixture Bravo"
    finally:
        other.catalog.close()


def test_live_projection_is_unreachable_from_existing_browser_and_owner_routes(harness, keys, assets, monkeypatch):
    fake = FakeEpic()
    app = create_mock_beta_app(
        harness.db,
        oidc_key=keys[0].public_key(),
        epic_key=keys[1],
        oidc_transport=httpx.MockTransport(harness.oidc),
        epic_transport=httpx.MockTransport(harness.epic),
        browser_assets_root=assets,
        live=LiveRefresh(
            key=keys[1],
            config=settings.EpicConfig("invented-live-client", Path("never-read.key"), "https://keys.invalid/jwks"),
            allowlist=LiveAllowlist({LIVE_ALIAS: LIVE_PATIENT}),
            transport=httpx.MockTransport(fake.respond),
        ),
        clock=lambda: harness.clock,
    )

    def never_called(*args, **kwargs):
        pytest.fail("internal LIVE record projection reached from an HTTP route")

    monkeypatch.setattr(projection, "live_record_view", never_called)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        harness.login(client)
        response = client.get(f"/beta/epic/patients/{LIVE_ALIAS}/record", headers=READ)
        assert response.status_code == 404 and response.json() == {"error": {"code": "patient_unavailable"}}
        assert response.headers["cache-control"] == "no-store"
        patients = client.get("/beta/epic/patients", headers=READ).json()
        assert {patient["alias"] for patient in patients["patients"]} == set(MOCK_PATIENTS)
        status = client.get(f"/beta/epic/patients/{LIVE_ALIAS}/status").json()
        assert status["serving"] == "unavailable" and status["publication"] == "disabled"
        assert "record" not in status and "provenance" not in status
        assert not fake.calls
    root = Path(__file__).resolve().parents[1]
    for filename in (
        "api/main.py",
        "api/beta_local.py",
        "scripts/run_beta_local.py",
        "frontend/beta/client.ts",
    ):
        source = (root / filename).read_text(encoding="utf-8")
        assert "beta_live_record" not in source and "live_record_view" not in source
    assert signature(create_mock_beta_app).parameters["synthetic_live_records"].default is False
