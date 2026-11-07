"""Conservative initial-profile staging through the existing Epic mapper and FHIR gate."""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any

from src.clinical.fhir_models import parse_timestamp, validate_resource
from src.interop.epic.beta_acquisition import PROFILE, Acquisition, AcquisitionError
from src.interop.epic.beta_catalog import POLICY_VERSION
from src.interop.epic.mapping import EpicMapper, MAPPING_VERSION, MappingReport, content_sha256
from src.interop.epic.workspace import count_dangling_references
from src.safety.guards import flag_content

SOURCE = "epic-sandbox-mock"
MANIFEST_VERSION = "mock-generation/1"
CLINICAL_TYPES = {"Patient", "Encounter", "Condition", "Observation", "DiagnosticReport"}
STATUS = {
    "Encounter": {"planned", "arrived", "triaged", "in-progress", "onleave", "finished", "cancelled", "unknown"},
    "Observation": {"registered", "preliminary", "final", "amended", "corrected", "cancelled", "unknown"},
    "DiagnosticReport": {
        "registered",
        "partial",
        "preliminary",
        "final",
        "amended",
        "corrected",
        "cancelled",
        "unknown",
    },
}


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def instant(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _check_source(resource: dict[str, Any], patient: str) -> None:
    kind = resource["resourceType"]
    if kind in STATUS and resource.get("status") not in STATUS[kind]:
        raise AcquisitionError("mapping_rejected")
    if kind == "Patient":
        try:
            date.fromisoformat(resource["birthDate"])
        except (ValueError, TypeError, KeyError):
            raise AcquisitionError("mapping_rejected") from None
    period = resource.get("period")
    if isinstance(period, dict) and period.get("start") and period.get("end"):
        try:
            if parse_timestamp(period["start"]) > parse_timestamp(period["end"]):
                raise AcquisitionError("clinical_conflict")
        except (ValueError, TypeError):
            raise AcquisitionError("mapping_rejected") from None
    if flag_content(json.dumps(resource)):
        raise AcquisitionError("unsafe_content")

    def references(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                references(item)
        elif isinstance(value, dict):
            reference = value.get("reference")
            if isinstance(reference, str):
                from src.interop.epic import settings

                relative = reference.removeprefix(settings.FHIR_BASE + "/")
                if relative.startswith("Patient/") and relative != f"Patient/{patient}":
                    raise AcquisitionError("patient_scope")
                if "/Patient/" in reference and reference != f"{settings.FHIR_BASE}/Patient/{patient}":
                    raise AcquisitionError("patient_scope")
            for item in value.values():
                references(item)

    references(resource)
    if kind == "DiagnosticReport":
        from src.interop.epic import settings
        from src.interop.epic.client import valid_fhir_id

        results = resource.get("result", [])
        if not isinstance(results, list):
            raise AcquisitionError("resource_scope")
        for result in results:
            reference = result.get("reference") if isinstance(result, dict) else None
            if not isinstance(reference, str):
                raise AcquisitionError("resource_scope")
            relative = reference.removeprefix(settings.FHIR_BASE + "/")
            if not relative.startswith("Observation/"):
                raise AcquisitionError("resource_scope")
            try:
                rid = valid_fhir_id(relative[len("Observation/") :])
                if rid in {".", ".."}:
                    raise ValueError
            except ValueError:
                raise AcquisitionError("resource_scope") from None


def normalize(acquisition: Acquisition, alias: str, gid: str) -> tuple[dict[str, Any], bytes]:
    acquisition.checkpoint()
    if acquisition.resources is None:
        raise AcquisitionError("candidate_unavailable")
    resources = list(acquisition.resources.values())
    retrieved_at = acquisition.catalog.clock()
    mapper = EpicMapper(acquisition.patient, snapshot_id=gid, retrieved_at=instant(retrieved_at))
    mapped: list[dict[str, Any]] = []
    report = MappingReport()
    metadata = []
    coverage: dict[str, dict[str, Any]] = copy.deepcopy(acquisition.operations)
    for counts in coverage.values():
        counts.update(imported=0, rejected=0, excluded=0, duplicates=counts["matches"] - counts["unique"])
        counts["import"] = "not_imported"
    for resource in resources:
        acquisition.checkpoint()
        _check_source(resource, acquisition.patient)
        kind = resource["resourceType"]
        if kind == "DocumentReference":
            metadata.append(resource)
            continue
        if kind not in CLINICAL_TYPES:
            raise AcquisitionError("resource_scope")
        try:
            outcome = mapper.map(resource)
        except (ValueError, TypeError, AttributeError):
            raise AcquisitionError("mapping_rejected") from None
        report.add(outcome)
        names = (
            [kind]
            if kind == "Patient"
            else [
                f"{kind}:{category}" if category else kind
                for planned, category in PROFILE
                if planned == kind
                and (
                    category is None
                    or any(
                        coding.get("code") == category
                        for concept in resource.get("category", [])
                        for coding in concept.get("coding", [])
                    )
                )
            ]
        )
        for name in names:
            coverage[name]["imported" if outcome.resource is not None else "rejected"] += 1
        if outcome.resource is None or validate_resource(outcome.resource):
            # No unexplained clinical mapping rejection is an approved MOCK exclusion.
            for name in names:
                coverage[name]["import"] = "partially_imported" if coverage[name]["imported"] else "not_imported"
            acquisition.operations = coverage
            raise AcquisitionError("mapping_rejected")
        mapped.append(outcome.resource)
    if sum(r["resourceType"] == "Patient" for r in mapped) != 1:
        raise AcquisitionError("patient_scope")
    for name, counts in coverage.items():
        if counts["execution"] != "succeeded":
            raise AcquisitionError("incomplete_candidate")
        if counts["matches"] == 0:
            counts["import"] = "returned_none"
        elif name == "DocumentReference:clinical-note":
            counts.update(
                excluded=counts["unique"], **{"import": "not_imported", "reason": "note_bodies_outside_policy"}
            )
        else:
            counts["import"] = "imported"
    for kind in ("MedicationRequest", "Binary"):
        coverage[kind] = {
            "execution": "not_requested",
            "import": "not_imported",
            "reason": "outside_initial_policy",
            "matches": 0,
            "unique": 0,
            "pages": 0,
            "imported": 0,
            "excluded": 0,
            "rejected": 0,
            "duplicates": 0,
        }
    report.dangling_references = count_dangling_references(mapped)
    acquisition.operations = coverage
    started = acquisition.catalog.db.execute(
        "SELECT started_at FROM attempt WHERE attempt_id = ?", (acquisition.attempt,)
    ).fetchone()[0]
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "generation_id": gid,
        "alias": alias,
        "source": SOURCE,
        "mode": "MOCK",
        "attempt_id": acquisition.attempt,
        "policy": POLICY_VERSION,
        "mapping_version": MAPPING_VERSION,
        "patient_id": mapper.patient_id,
        "acquisition_started_at": started,
        "retrieved_at": retrieved_at,
        "resource_counts": dict(Counter(r["resourceType"] for r in mapped)),
        "coverage": {
            "operations": coverage,
            "unresolved_references": report.dangling_references,
            "mapping": report.as_dict(),
            "note_bodies": "not_imported",
        },
        "document_metadata": metadata,
    }
    bundle = encoded(
        {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {"resource": resource}
                for resource in sorted(mapped, key=lambda item: (item["resourceType"], item["id"]))
            ],
        }
    )
    manifest["bundle_sha256"] = content_sha256(bundle)
    return manifest, bundle
