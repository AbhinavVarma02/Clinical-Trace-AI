"""Epic -> Clinical-Trace mapping, provenance and field status (MOCK/offline: invented fixtures only)."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from src.clinical.fhir_client import PatientScopeViolation
from src.clinical.fhir_models import parse_timestamp, validate_resource
from src.interop.epic import mapping, settings
from src.interop.epic.client import BinaryContent

FIXTURE = Path(__file__).parent / "fixtures" / "epic_sandbox" / "epic_shaped_patients.json"
RETRIEVED_AT = "2026-11-05T18:00:00Z"
CLINICAL_TIME_FIELDS = ("effectiveDateTime", "onsetDateTime", "recordedDate", "authoredOn", "date")


def load(name: str = "alpha") -> tuple[str, list[dict], dict[str, BinaryContent]]:
    patient = json.loads(FIXTURE.read_text(encoding="utf-8"))["patients"][name]
    binaries = {
        ref: BinaryContent(reference=ref, content_type=item["contentType"], data=item["text"].encode())
        for ref, item in patient["binaries"].items()
    }
    return patient["patient_id"], patient["resources"], binaries


def map_all(name: str = "alpha") -> tuple[mapping.EpicMapper, dict[str, mapping.MappedResource], list[dict]]:
    pid, resources, binaries = load(name)
    mapper = mapping.EpicMapper(pid, snapshot_id="snap-test", retrieved_at=RETRIEVED_AT)
    return mapper, {r["id"]: mapper.map(r, binaries) for r in resources}, resources


def provenance(resource: dict) -> dict[str, list]:
    ext = next(e for e in resource["extension"] if e["url"] == mapping.PROVENANCE_URL)
    parts: dict[str, list] = {}
    for item in ext["extension"]:
        value = next(v for k, v in item.items() if k.startswith("value"))
        parts.setdefault(item["url"], []).append(value)
    return parts


def test_accepted_resources_pass_the_existing_validator_with_namespaced_ids():
    mapper, outcomes, _ = map_all()
    accepted = [o.resource for o in outcomes.values() if o.accepted]
    assert len(accepted) == 12
    assert re.fullmatch(r"^synthetic_[a-z0-9]{1,32}$", mapper.patient_id)  # fits the existing API contract
    for resource in accepted:
        assert validate_resource(resource) == []
        assert resource["meta"]["versionId"] == "1"
        if resource["resourceType"] != "Patient":
            assert resource["id"].startswith("epic-") and resource["subject"] == {
                "reference": f"Patient/{mapper.patient_id}"
            }
    again = mapping.EpicMapper(mapper.source_patient_id, snapshot_id="other", retrieved_at=RETRIEVED_AT)
    assert again.patient_id == mapper.patient_id  # deterministic across snapshots


def test_rejections_are_explicit_and_never_filled_in():
    _, outcomes, _ = map_all()
    rejected = {sid: o.issues for sid, o in outcomes.items() if not o.accepted}
    assert rejected == {
        "eFixtureObsPeriod4": ["effective-period-unsupported"],
        "eFixtureObsPending5": ["missing-value"],
        "eFixtureDocRtfOnly2": ["note-format-unsupported"],
    }


def test_every_source_field_is_original_or_has_exactly_one_explicit_status():
    _, outcomes, sources = map_all()
    for source in sources:
        outcome = outcomes[source["id"]]
        if not outcome.accepted:
            continue
        mapped, statuses = outcome.resource, provenance(outcome.resource)
        labelled = {
            status: [str(e).split(":")[0].split(" ")[0].split("<-")[0] for e in statuses.get(status, [])]
            for status in ("transformed", "substituted", "unavailable", "unsupported")
        }
        for key, value in source.items():
            if key in ("resourceType", "id", "meta", "subject"):
                continue
            hits = [
                s
                for s, paths in labelled.items()
                if any(p == key or p.startswith(f"{key}.") or p.startswith(f"{key}[") for p in paths)
            ]
            if mapped.get(key) == value:
                assert not ({"unsupported", "unavailable"} & set(hits)), (source["id"], key)
            else:
                assert hits, f"{source['id']}.{key} changed without a recorded status"


def test_provenance_preserves_source_identity_version_time_and_hash():
    _, outcomes, sources = map_all()
    encounter = outcomes["eFixtureEncInpatient1"].resource
    parts = provenance(encounter)
    assert parts["sourceReference"] == ["Encounter/eFixtureEncInpatient1"]
    assert parts["sourceVersionId"] == ["3"] and parts["sourceLastUpdated"] == ["2025-03-07T10:00:00Z"]
    assert parts["sourceContentSha256"] == [
        mapping.content_sha256(next(s for s in sources if s["id"] == "eFixtureEncInpatient1"))
    ]
    assert parts["retrievedAt"] == [RETRIEVED_AT] and parts["mappingVersion"] == [mapping.MAPPING_VERSION]
    assert encounter["meta"]["source"] == f"{settings.FHIR_BASE}/Encounter/eFixtureEncInpatient1/_history/3"
    assert encounter["meta"]["lastUpdated"] == "2025-03-07T10:00:00Z"
    assert {t["code"] for t in encounter["meta"]["tag"]} == {"synthetic", settings.SOURCE_SYSTEM}


def test_retrieval_time_is_never_a_clinical_time_and_is_labelled_when_used_as_record_metadata():
    _, outcomes, _ = map_all()
    retrieved = parse_timestamp(RETRIEVED_AT)
    for outcome in outcomes.values():
        if not outcome.accepted:
            continue
        resource = outcome.resource
        for name in CLINICAL_TIME_FIELDS:
            if name in resource:
                assert parse_timestamp(resource[name]) != retrieved
        for part in ("start", "end"):
            if part in (resource.get("period") or {}):
                assert parse_timestamp(resource["period"][part]) != retrieved
        parts = provenance(resource)
        if resource["meta"]["lastUpdated"] == RETRIEVED_AT:
            assert "meta.lastUpdated" in parts["unavailable"]
            assert any("record metadata only" in s for s in parts["substituted"])
            assert "sourceLastUpdated" not in parts


def test_partial_onset_is_moved_not_expanded_and_icd10_is_listed_first():
    _, outcomes, _ = map_all()
    problem = outcomes["eFixtureCondProblem1"].resource
    assert "onsetDateTime" not in problem and problem["onsetString"] == "2019"
    assert problem["code"]["coding"][0]["code"] == "E11.9" and len(problem["code"]["coding"]) == 2
    parts = provenance(problem)
    assert "onsetDateTime" in parts["unavailable"]
    assert "code.coding:reordered-icd10-first" in parts["transformed"]


def test_condition_without_icd10_never_asserts_another_code_as_icd10():
    _, outcomes, _ = map_all()
    snomed_only = outcomes["eFixtureCondSnomedOnly3"].resource
    assert "coding" not in snomed_only["code"] and snomed_only["code"]["text"] == "Hypertensive disorder"
    parts = provenance(snomed_only)
    assert any(p.startswith("code.coding (no icd10 coding") for p in parts["unsupported"])


def test_observation_codes_categories_and_component_values():
    _, outcomes, _ = map_all()
    crp = outcomes["eFixtureObsCrp1"].resource
    assert crp["code"]["coding"][0]["system"] == "http://loinc.org"
    assert crp["category"][0]["coding"][0]["code"] == "laboratory"
    bp = outcomes["eFixtureObsBp1"].resource
    assert bp["valueString"] == "Systolic blood pressure 132 mmHg; Diastolic blood pressure 84 mmHg"
    assert "component:rendered-as-valueString" in provenance(bp)["transformed"]


def test_note_html_is_converted_to_plain_text_with_source_hash():
    _, outcomes, _ = map_all()
    note = outcomes["eFixtureDocDischarge1"].resource
    attachment = note["content"][0]["attachment"]
    assert attachment["contentType"] == "text/plain" and len(note["content"]) == 1
    text = base64.b64decode(attachment["data"]).decode()
    assert text.splitlines()[0].startswith("Hospital course: Treated for community-acquired pneumonia")
    assert "Assessment: Improving. CRP fell from 84 mg/L to 12 mg/L." in text.splitlines()
    assert "ignored" not in text and "<" not in text
    assert any(
        t.startswith("content:html-to-text from Binary/eFixtureBinNote1") for t in provenance(note)["transformed"]
    )
    assert (
        note["context"]["encounter"][0]["reference"]
        == f"Encounter/{mapping.internal_id('Encounter', 'eFixtureEncInpatient1')}"
    )


def test_missing_binary_rejects_the_note_instead_of_inventing_text():
    pid, resources, _ = load()
    mapper = mapping.EpicMapper(pid, snapshot_id="s", retrieved_at=RETRIEVED_AT)
    note = next(r for r in resources if r["id"] == "eFixtureDocDischarge1")
    outcome = mapper.map(note, {})
    assert not outcome.accepted and outcome.issues == ["note-content-unavailable"]


def test_medication_reference_display_is_a_labelled_substitution():
    _, outcomes, _ = map_all()
    med = outcomes["eFixtureMedAmox1"].resource
    assert med["medicationCodeableConcept"] == {"text": "amoxicillin 500 mg capsule"}
    assert "medicationReference" not in med
    assert "medicationCodeableConcept.text<-medicationReference.display" in provenance(med)["substituted"]
    assert med["authoredOn"] == "2025-03-06T15:00:00Z"


def test_references_are_rewritten_to_internal_ids_or_absolute_source_urls():
    _, outcomes, _ = map_all()
    enc = outcomes["eFixtureEncInpatient1"].resource
    assert "serviceProvider" not in enc and "location" not in enc  # not carried: listed as unsupported
    assert {"serviceProvider", "location"} <= set(provenance(enc)["unsupported"])
    report = outcomes["eFixtureDxrCrp1"].resource
    assert report["result"][0]["reference"] == f"Observation/{mapping.internal_id('Observation', 'eFixtureObsCrp1')}"
    note = outcomes["eFixtureDocDischarge1"].resource
    assert note["author"][0]["reference"] == f"{settings.FHIR_BASE}/Practitioner/eFixturePract1"
    follow_up = outcomes["eFixtureEncFollowUp2"].resource
    assert follow_up["class"]["display"] == "AMB"
    assert "class.display<-class.code" in provenance(follow_up)["substituted"]


def test_patient_minimises_unsupported_demographics():
    mapper, outcomes, _ = map_all()
    patient = outcomes["eFixtureAlpha0001-NotReal"].resource
    assert patient["id"] == mapper.patient_id
    assert set(patient) == {"resourceType", "id", "meta", "extension", "name", "gender", "birthDate"}
    assert {"identifier", "telecom", "address", "extension"} <= set(provenance(patient)["unsupported"])


def test_cross_patient_resources_raise_instead_of_mapping():
    pid, resources, _ = load()
    _, bravo, _ = load("bravo")
    mapper = mapping.EpicMapper(pid, snapshot_id="s", retrieved_at=RETRIEVED_AT)
    with pytest.raises(PatientScopeViolation):
        mapper.map(bravo[1])
    with pytest.raises(PatientScopeViolation):
        mapper.map(bravo[0])


def test_mapping_report_counts_without_content():
    _, outcomes, _ = map_all()
    report = mapping.MappingReport()
    for outcome in outcomes.values():
        report.add(outcome)
    summary = report.as_dict()
    assert summary["received"]["Observation"] == 5 and summary["mapped"]["Observation"] == 3
    assert summary["rejected"]["Observation:effective-period-unsupported"] == 1
    assert summary["field_status"]["substituted"] >= 3
    rendered = json.dumps(summary)
    assert "pneumonia" not in rendered.lower() and "eFixture" not in rendered


def test_html_to_text_handles_blocks_entities_and_hidden_content():
    assert mapping.html_to_text("<div>A &amp; B</div><br><p> C  D </p><script>x</script>") == "A & B\nC D"


def test_retrieved_at_must_be_a_full_instant():
    with pytest.raises(ValueError):
        mapping.EpicMapper("x", snapshot_id="s", retrieved_at="2026-11")
