"""Browser record route (offline): one passive, MOCK-only, patient-isolated view of the active generation.

Uses the existing MOCK Epic double with all eight approved operation profiles, so conditions, encounters, labs,
vital signs, diagnostic reports and note metadata come from a real published generation, not from a fixture.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from api.beta import create_mock_beta_app
from src.interop.epic.mapping import internal_id
from tests.test_beta_browser_bootstrap import READ, assets, denied  # noqa: F401 - `assets` is a fixture
from tests.test_beta_generations import BODY, generations, harness, keys  # noqa: F401 - fixtures
from tests.test_beta_owner_refresh import ALPHA, BRAVO, ORIGIN, PATIENT_A, PATIENT_B

RECORD = "/beta/epic/patients/{alias}/record"
ALPHA_RECORD = RECORD.format(alias="mock-alpha")
BRAVO_RECORD = RECORD.format(alias="mock-bravo")
SECTIONS = ("conditions", "encounters", "labs", "vitals", "reports", "notes")


@pytest.fixture
def app(generations, keys, assets):  # noqa: F811
    h = generations

    def client():
        return TestClient(
            create_mock_beta_app(
                h.db,
                oidc_key=keys[0].public_key(),
                epic_key=keys[1],
                oidc_transport=httpx.MockTransport(h.oidc),
                epic_transport=httpx.MockTransport(h.epic),
                generation_root=h.generation_root,
                limits=h.limits,
                clock=lambda: h.clock,
                browser_assets_root=assets,
            ),
            base_url=ORIGIN,
            follow_redirects=False,
        )

    return h, client


def read(client, path, **headers):
    return client.get(path, headers={**READ, **headers})


def strip_name(view):
    """The invented display name embeds the upstream fixture id, so it is excluded from leakage searches."""
    text = json.loads(json.dumps(view))
    text["record"]["patient"]["display_name"] = None
    return json.dumps(text)


def keys_of(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys_of(item)


# -- access --------------------------------------------------------------------------------------------------


def test_record_needs_the_owner_session_and_the_browser_read_profile(app):
    h, client = app
    with client() as c:
        denied(read(c, ALPHA_RECORD), 401, "owner_required")
        h.login(c)
        assert read(c, ALPHA_RECORD).status_code == 200
        for name in READ:
            headers = {key: value for key, value in READ.items() if key != name}
            denied(c.get(ALPHA_RECORD, headers=headers), 403, "same_origin_required")
        denied(c.post(ALPHA_RECORD, headers={**READ, "Origin": ORIGIN}), 405, "method_not_allowed")
        denied(c.get(ALPHA_RECORD + "?alias=mock-bravo", headers=READ), 400, "unexpected_payload")
    assert not h.calls  # a read never reaches Epic


@pytest.mark.parametrize("alias", ["mock-charlie", "live-synthetic-1", "constructor", "__proto__", "MOCK-ALPHA"])
def test_only_the_two_mock_aliases_exist(app, alias):
    h, client = app
    with client() as c:
        h.login(c)
        denied(read(c, RECORD.format(alias=alias)), 404, "patient_unavailable")


def test_a_revoked_patient_has_no_record(app):
    h, client = app
    with client() as c:
        h.refresh_generation(c)
        assert read(c, ALPHA_RECORD).json()["record"] is not None
        assert h.post(c, ALPHA + "/revoke").status_code == 204
        denied(read(c, ALPHA_RECORD), 404, "patient_unavailable")


def test_reading_a_record_is_passive_and_does_not_extend_the_session(app):
    h, client = app
    with client() as c:
        h.login(c)
        before = read(c, "/beta/auth/session").json()["last_activity_at_unix_s"]
        h.clock += 100
        assert read(c, ALPHA_RECORD).status_code == 200
        assert read(c, "/beta/auth/session").json()["last_activity_at_unix_s"] == before


# -- content -------------------------------------------------------------------------------------------------


def test_before_any_refresh_the_view_says_so_and_carries_no_record(app):
    h, client = app
    with client() as c:
        h.login(c)
        view = read(c, ALPHA_RECORD).json()
    assert (view["mode"], view["source"], view["data_origin"]) == (
        "MOCK",
        "epic-sandbox-mock",
        "synthetic_mock_transport",
    )
    assert view["serving"] == "unavailable" and view["active"] is None and view["attempt"] is None
    assert view["record"] is None and view["coverage"] is None and view["provenance"] is None
    assert [w["code"] for w in view["warnings"]] == ["synthetic_mock", "no_generation"]


def test_a_published_generation_shows_every_section_with_status_coverage_and_provenance(app):
    h, client = app
    with client() as c:
        status = h.refresh_generation(c)
        view = read(c, ALPHA_RECORD).json()
    gid = status["active"]["generation_id"]
    assert view["serving"] == "fresh" and view["active"]["generation_id"] == gid
    assert view["attempt"]["state"] == "succeeded" and view["attempt"]["requests"] >= 8
    assert set(view["attempt"]["operations"]) >= {
        "Patient",
        "Encounter",
        "Condition:problem-list-item",
        "Condition:encounter-diagnosis",
        "Observation:laboratory",
        "Observation:vital-signs",
        "DocumentReference:clinical-note",
        "DiagnosticReport",
    }
    record = view["record"]
    assert {name: record[name]["total"] for name in SECTIONS} == {name: 1 for name in SECTIONS} | {"conditions": 2}
    assert record["patient"]["synthetic"] is not None and record["patient"]["gender"] == "male"
    assert {c["category"] for c in record["conditions"]["items"]} == {"problem-list-item", "encounter-diagnosis"}
    lab, vital = record["labs"]["items"][0], record["vitals"]["items"][0]
    assert (lab["name"], lab["value"], lab["unit"]) == ("Creatinine", 1.2, "mg/dL")
    assert (vital["name"], vital["value"], vital["unit"]) == ("Heart rate", 70, "/min")
    note = record["notes"]["items"][0]
    assert note["type"] == "Clinical note" and note["category"] == "clinical-note"
    assert record["reports"]["items"][0]["name"] == "Cardiac stress test"
    assert record["reports"]["items"][0]["result_count"] == 2
    for section in SECTIONS:
        assert record[section]["truncated"] is False
        for item in record[section]["items"]:
            if section != "notes":
                assert item["source"]["reference"] and item["source"]["version"]
    coverage = view["coverage"]
    assert coverage["sources"]["MedicationRequest"] == "not_imported"
    assert coverage["sources"]["DocumentReference.metadata"] == "returned"
    assert coverage["operations"]["DocumentReference:clinical-note"]["import"] == "not_imported"
    assert coverage["operations"]["MedicationRequest"]["execution"] == "not_requested"
    assert coverage["unresolved_references"] == 1 and coverage["note_bodies"] == "not_imported"
    provenance = view["provenance"]
    assert provenance["generation_id"] == gid and provenance["mode"] == "MOCK"
    assert provenance["manifest_version"] and provenance["mapping_version"] and len(provenance["bundle_sha256"]) == 64
    codes = {w["code"] for w in view["warnings"]}
    assert {"synthetic_mock", "note_bodies_not_imported", "medications_not_imported", "unresolved_references"} <= codes
    assert "serving_fresh" not in codes


def test_the_view_never_contains_note_bodies_attachments_bearers_or_upstream_ids(app):
    h, client = app
    with client() as c:
        h.refresh_generation(c)
        view = read(c, ALPHA_RECORD).json()
    text = strip_name(view)
    for forbidden in (BODY, "Binary/secret", "invented-fhir-bearer", PATIENT_A, PATIENT_B, "upstream-7"):
        assert forbidden not in text
    assert not {"content", "attachment", "data", "url", "author"} & set(keys_of(view))


def test_each_patient_sees_only_its_own_record(app):
    h, client = app
    with client() as c:
        h.refresh_generation(c, ALPHA)
        h.refresh_generation(c, BRAVO)
        alpha, bravo = read(c, ALPHA_RECORD).json(), read(c, BRAVO_RECORD).json()
    assert alpha["alias"] == "mock-alpha" and bravo["alias"] == "mock-bravo"
    assert alpha["active"]["generation_id"] != bravo["active"]["generation_id"]
    assert alpha["record"]["patient"]["display_name"] != bravo["record"]["patient"]["display_name"]
    own = internal_id("Patient", PATIENT_A)
    other = internal_id("Patient", PATIENT_B)
    assert own in json.dumps(alpha) or own in alpha["record"]["patient"]["source"]["reference"]
    assert other not in json.dumps(alpha) and own not in json.dumps(bravo)
    # The two fixture patients reuse upstream resource ids on purpose, so only the patient resources differ by id;
    # isolation is that each response is built from its own generation and its own patient-scoped store.
    assert alpha["record"]["patient"]["source"]["reference"] != bravo["record"]["patient"]["source"]["reference"]


# -- degraded states -----------------------------------------------------------------------------------------


def test_an_old_generation_is_labelled_stale(app):
    h, client = app
    with client() as c:
        h.refresh_generation(c)
        h.clock += 90_000  # past the 24 h freshness window
        h.login(c)
        view = read(c, ALPHA_RECORD).json()
    assert view["serving"] == "stale" and view["record"] is not None
    assert "serving_stale" in {w["code"] for w in view["warnings"]}


def test_a_failed_refresh_keeps_the_earlier_generation_and_says_so(app):
    h, client = app
    with client() as c:
        h.refresh_generation(c)
        h.clock += 3601
        h.login(c)

        async def broken(request):
            return httpx.Response(500) if "/Observation" in request.url.path else None

        h.attack = broken
        assert h.post(c, ALPHA + "/refresh", key="failing-refresh-key").status_code == 202
        h.wait(c, ALPHA)
        view = read(c, ALPHA_RECORD).json()
    assert view["record"] is not None and view["attempt"]["state"] != "succeeded"
    assert "last_attempt_failed" in {w["code"] for w in view["warnings"]}


def test_a_tampered_generation_is_unavailable_without_a_record(app):
    h, client = app
    with client() as c:
        status = h.refresh_generation(c)
        manifest = h.generation_root / "objects" / status["active"]["generation_id"] / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        view = read(c, ALPHA_RECORD).json()
    assert view["serving"] == "unavailable" and view["evidence_error"] == "generation_integrity"
    assert view["record"] is None and view["coverage"] is None and view["provenance"] is None
    assert "serving_unavailable" in {w["code"] for w in view["warnings"]}


def test_without_a_generation_store_the_route_still_answers_truthfully(harness, keys, assets):  # noqa: F811
    h = harness
    app = create_mock_beta_app(
        h.db,
        oidc_key=keys[0].public_key(),
        epic_key=keys[1],
        oidc_transport=httpx.MockTransport(h.oidc),
        epic_transport=httpx.MockTransport(h.epic),
        clock=lambda: h.clock,
        browser_assets_root=assets,
    )
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as c:
        h.login(c)
        view = read(c, ALPHA_RECORD).json()
    assert view["record"] is None and view["serving"] == "unavailable"
