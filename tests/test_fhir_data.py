"""Synthetic FHIR fixture, validation, versioned ingestion, and FHIR client adapter."""

from __future__ import annotations

import base64
import copy
import json

import httpx
import pytest

from src.clinical.fhir_client import FHIRClientError, FHIRHttpClient, PatientScopeViolation, searchset_bundle
from src.clinical.fhir_models import decode_attachment, normalize_timestamp, validate_resource
from src.clinical.records import ClinicalRecords
from src.clinical.risk_bridge import assess, icd10_to_icd9_proxy
from src.clinical.store import FHIRStore
from src.clinical.synthetic import FIXTURE_PATH, build_bundle
from src.persistence.db import Database


@pytest.fixture()
def store() -> FHIRStore:
    s = FHIRStore(Database(":memory:"))
    s.ingest_bundle_file(FIXTURE_PATH)
    return s


def test_committed_fixture_matches_generator() -> None:
    committed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(build_bundle(), sort_keys=True))


def test_every_resource_is_valid_and_tagged_synthetic() -> None:
    bundle = build_bundle()
    assert len(bundle["entry"]) > 50
    for entry in bundle["entry"]:
        resource = entry["resource"]
        assert validate_resource(resource) == [], resource["id"]
        assert any(tag["code"] == "synthetic" for tag in resource["meta"]["tag"])


def test_patients_have_synthetic_ids_and_names() -> None:
    patients = [e["resource"] for e in build_bundle()["entry"] if e["resource"]["resourceType"] == "Patient"]
    assert len(patients) >= 3
    for patient in patients:
        assert patient["id"].startswith("synthetic_")
        assert "synthetic" in patient["name"][0]["text"].lower()


def test_ingestion_is_idempotent(store: FHIRStore) -> None:
    again = store.ingest_bundle_file(FIXTURE_PATH)
    assert again.inserted == 0
    assert again.duplicates == again.received


def test_conflicting_version_is_rejected(store: FHIRStore) -> None:
    resource = copy.deepcopy(store.get("Observation", "p001-o1"))
    resource["valueQuantity"]["value"] = 99.0
    report = store.ingest([resource], source="test")
    assert report.invalid == 1 and report.inserted == 0
    assert "conflict" in report.issues[0]["issue"]
    assert store.get("Observation", "p001-o1")["valueQuantity"]["value"] == 7.2


def test_new_version_becomes_current_and_history_is_kept(store: FHIRStore) -> None:
    resource = copy.deepcopy(store.get("Observation", "p001-o1"))
    resource["meta"]["versionId"] = "2"
    resource["meta"]["lastUpdated"] = "2025-12-01T00:00:00Z"
    resource["valueQuantity"]["value"] = 7.3
    report = store.ingest([resource], source="test")
    assert report.inserted == 1
    assert store.get("Observation", "p001-o1")["valueQuantity"]["value"] == 7.3
    assert [r["meta"]["versionId"] for r in store.history("Observation", "p001-o1")] == ["1", "2"]
    assert store.get("Observation", "p001-o1", version=1)["valueQuantity"]["value"] == 7.2


def test_invalid_resources_are_rejected_with_issues(store: FHIRStore) -> None:
    bad_patient = {"resourceType": "Patient", "id": "real_person", "meta": {"versionId": "1",
                   "lastUpdated": "2025-01-01T00:00:00Z"}, "gender": "male", "birthDate": "1950-01-01"}
    unsupported = {"resourceType": "Claim", "id": "x"}
    report = store.ingest([bad_patient, unsupported], source="test")
    assert report.invalid == 2
    assert any("synthetic" in i["issue"] for i in report.issues)


def test_timestamps_are_normalised_to_utc() -> None:
    assert normalize_timestamp("2025-07-02T18:20:00-04:00") == "2025-07-02T22:20:00Z"
    assert normalize_timestamp("2025-01-15") == "2025-01-15T00:00:00Z"


def test_attachment_decoding_rejects_unsafe_content() -> None:
    document = next(e["resource"] for e in build_bundle()["entry"]
                    if e["resource"]["resourceType"] == "DocumentReference")
    assert "Reason for admission" in decode_attachment(document)
    html = copy.deepcopy(document)
    html["content"][0]["attachment"]["contentType"] = "text/html"
    assert validate_resource(html)
    huge = copy.deepcopy(document)
    huge["content"][0]["attachment"]["data"] = base64.b64encode(b"a" * 70_000).decode()
    assert any("size" in issue for issue in validate_resource(huge))


def test_records_views_keep_source_references(store: FHIRStore) -> None:
    records = ClinicalRecords(store)
    timeline = records.timeline("synthetic_p001")
    assert timeline == sorted(timeline, key=lambda e: (e["when"] or "", e["kind"]))
    assert all(event["source"]["reference"] for event in timeline)
    hba1c = next(s for s in records.lab_trends("synthetic_p001") if s["loinc"] == "4548-4")
    assert [p["value"] for p in hba1c["points"]] == [7.2, 8.1, 8.9, 9.4]
    assert hba1c["direction"] == "up"


def test_missing_information_detects_incomplete_journey(store: FHIRStore) -> None:
    categories = {f["category"] for f in ClinicalRecords(store).missing_information("synthetic_p003")}
    assert {"encounter_end_missing", "discharge_summary_missing", "unit_missing",
            "medication_list_missing"} <= categories
    assert ClinicalRecords(store).missing_information("synthetic_p001") == []


def test_risk_bridge_eligibility(store: FHIRStore) -> None:
    records = ClinicalRecords(store)
    p001 = assess(records, "synthetic_p001")
    assert p001["available"] and p001["encounter_id"] == "p001-e4"
    assert p001["features"]["number_inpatient"] == 2
    assert any("Out of distribution" in c for c in p001["caveats"])
    p002 = assess(records, "synthetic_p002")
    assert not p002["available"] and any("diabetes" in r for r in p002["reasons"])
    p003 = assess(records, "synthetic_p003")
    assert not p003["available"] and any("discharge time" in r for r in p003["reasons"])
    assert icd10_to_icd9_proxy("E11.9") == "250" and icd10_to_icd9_proxy("I50.9") == "428"


def _mock_client(store: FHIRStore, handler_override=None) -> FHIRHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if handler_override:
            response = handler_override(request)
            if response is not None:
                return response
        path = request.url.path.removeprefix("/fhir")
        params = dict(request.url.params)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2:
            resource = store.get(parts[0], parts[1])
            return httpx.Response(200 if resource else 404, json=resource or {})
        bundle = searchset_bundle(store, "http://mock/fhir", parts[0], params["patient"],
                                  int(params.get("_count", 50)), int(params.get("_offset", 0)))
        return httpx.Response(200, json=bundle)

    return FHIRHttpClient("http://mock/fhir", transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_http_client_paginates_and_pulls_patient(store: FHIRStore) -> None:
    client = _mock_client(store)
    observations = client.search("Observation", "synthetic_p001", count=4)
    assert len(observations) == store.search("synthetic_p001", "Observation")[1]
    pulled = client.pull_patient("synthetic_p004")
    target = FHIRStore(Database(":memory:"))
    report = target.ingest(pulled, source="mock-fhir")
    assert report.invalid == 0 and report.inserted == len(pulled)


def test_http_client_rejects_cross_patient_results(store: FHIRStore) -> None:
    def leak(request: httpx.Request):
        if "patient=synthetic_p001" in str(request.url):
            bundle = searchset_bundle(store, "http://mock/fhir", "Observation", "synthetic_p002", 50, 0)
            return httpx.Response(200, json=bundle)
        return None

    with pytest.raises(PatientScopeViolation):
        _mock_client(store, leak).search("Observation", "synthetic_p001")


def test_http_client_retries_then_fails(store: FHIRStore) -> None:
    calls = {"n": 0}

    def flaky(_: httpx.Request):
        calls["n"] += 1
        return httpx.Response(503)

    client = _mock_client(store, flaky)
    with pytest.raises(FHIRClientError):
        client.read("Patient", "synthetic_p001")
    assert calls["n"] == client.max_retries + 1
