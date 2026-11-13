"""Browser-safe projection of one active MOCK generation (status, coverage, provenance, warnings and record).

Every field is copied by name from the verified generation (manifest and patient-scoped clinical views); nothing is
passed through wholesale. Upstream identifiers, document attachments and note bodies are never part of the view. This
module serves MOCK generations only: LIVE data has no browser route, and real promotion stays disabled.
"""

from __future__ import annotations

from typing import Any

from src.clinical.fhir_models import display_text, normalize_timestamp
from src.interop.epic.beta_catalog import MOCK_PATIENTS

LABELS = {"mock-alpha": "MOCK Alpha", "mock-bravo": "MOCK Bravo"}
LIMIT = 500  # per section; a longer section is cut and flagged
ATTEMPT_FIELDS = ("state", "policy", "admitted_at", "started_at", "finished_at", "error_code", "requests")
OPERATION_FIELDS = (
    "execution",
    "import",
    "reason",
    "matches",
    "pages",
    "unique",
    "imported",
    "excluded",
    "rejected",
    "duplicates",
)
PROVENANCE_FIELDS = (
    "generation_id",
    "manifest_version",
    "mapping_version",
    "policy",
    "retrieved_at",
    "acquisition_started_at",
    "bundle_sha256",
    "resource_counts",
)


def _source(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source") or {}
    return {key: source.get(key) for key in ("reference", "version", "last_updated", "synthetic")}


def _cut(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items[:LIMIT], "total": len(items), "truncated": len(items) > LIMIT}


def _attempt(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    operations = value.get("operations") or {}
    return {
        **{key: value.get(key) for key in ATTEMPT_FIELDS},
        "operations": {
            name: {key: counts.get(key) for key in OPERATION_FIELDS} for name, counts in sorted(operations.items())
        },
        "note_bodies": value.get("note_bodies", "not_imported"),
        "note_body_reason": value.get("note_body_reason", "outside_initial_policy"),
    }


def _metadata(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Document metadata only: type, title, date, category and status. No attachment, author or upstream id."""
    items = []
    for resource in resources:
        try:
            date = normalize_timestamp(str(resource["date"])) if resource.get("date") else None
        except (ValueError, TypeError):
            date = None
        categories = [
            coding.get("code")
            for concept in resource.get("category") or []
            if isinstance(concept, dict)
            for coding in concept.get("coding") or []
            if isinstance(coding, dict)
        ]
        items.append(
            {
                "type": display_text(resource.get("type")),
                "title": resource.get("description") or display_text(resource.get("type")),
                "date": date,
                "category": categories[0] if categories else None,
                "status": resource.get("status"),
            }
        )
    return sorted(items, key=lambda item: item["date"] or "")


def _warnings(view: dict[str, Any], manifest: dict[str, Any] | None) -> list[dict[str, str]]:
    found = [
        {
            "code": "synthetic_mock",
            "severity": "info",
            "message": "Synthetic MOCK data from an in-process transport. This is not Epic data and not a real patient.",
        }
    ]
    if view["active"] is None:
        found.append(
            {
                "code": "no_generation",
                "severity": "info",
                "message": "No refresh has produced a verified generation for this patient yet, so no record is shown.",
            }
        )
    elif view["serving"] != "fresh":
        found.append(
            {
                "code": f"serving_{view['serving']}",
                "severity": "warning",
                "message": f"This generation is {view['serving'].replace('_', ' ')}, not the result of the latest refresh.",
            }
        )
    attempt = view["attempt"]
    if attempt and attempt["state"] not in {"succeeded", "queued", "running"}:
        found.append(
            {
                "code": "last_attempt_failed",
                "severity": "warning",
                "message": "The most recent refresh did not succeed; the data shown is from an earlier generation.",
            }
        )
    if manifest is None:
        return found
    coverage = manifest["coverage"]
    operations = coverage["operations"].values()
    found.append(
        {
            "code": "note_bodies_not_imported",
            "severity": "warning",
            "message": "Note bodies are never imported; only note metadata is shown.",
        }
    )
    found.append(
        {
            "code": "medications_not_imported",
            "severity": "warning",
            "message": "Medications are not imported, so their absence here is not evidence of absence.",
        }
    )
    if any(op.get("rejected") or op.get("import") == "partially_imported" for op in operations):
        found.append(
            {
                "code": "partial_import",
                "severity": "warning",
                "message": "Some returned resources were rejected by mapping; the record may be incomplete.",
            }
        )
    if coverage.get("unresolved_references"):
        found.append(
            {
                "code": "unresolved_references",
                "severity": "warning",
                "message": f"{coverage['unresolved_references']} reference(s) point at resources that were not imported.",
            }
        )
    if any(op.get("duplicates") for op in operations):
        found.append(
            {"code": "duplicates_removed", "severity": "info", "message": "Duplicate search matches were collapsed."}
        )
    statuses = (coverage.get("mapping") or {}).get("field_status") or {}
    for name in ("substituted", "unavailable", "unsupported"):
        if statuses.get(name):
            found.append(
                {
                    "code": f"fields_{name}",
                    "severity": "info",
                    "message": f"{statuses[name]} field(s) were {name} by mapping rather than copied from the source.",
                }
            )
    return found


def _record(runtime: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    patient = manifest["patient_id"]
    records = runtime.records
    profile = records.profile(patient) or {}
    categories = {}
    for condition in runtime.store.all_for_patient(patient, "Condition"):
        concepts = condition.get("category") or []
        codes = [c.get("code") for concept in concepts for c in (concept.get("coding") or [])]
        categories[condition["id"]] = codes[0] if codes else None
    observation = lambda item: {  # noqa: E731 - one shared field list for labs and vital signs
        "name": item["name"],
        "loinc": item["loinc"],
        "value": item["value"],
        "unit": item["unit"],
        "unit_missing": item["unit_missing"],
        "effective": item["effective"],
        "source": _source(item),
    }
    return {
        "patient": {
            "display_name": profile.get("display_name"),
            "gender": profile.get("gender"),
            "birth_date": profile.get("birth_date"),
            "age_at_last_encounter": profile.get("age_at_last_encounter"),
            "synthetic": profile.get("synthetic"),
            "source": _source(profile),
        },
        "conditions": _cut(
            [
                {
                    "display": item["display"],
                    "category": categories.get(item["id"]),
                    "clinical_status": item["clinical_status"],
                    "onset": item["onset"],
                    "source": _source(item),
                }
                for item in records.conditions(patient)
            ]
        ),
        "encounters": _cut(
            [
                {
                    "class": item["class"],
                    "class_display": item["class_display"],
                    "start": item["start"],
                    "end": item["end"],
                    "length_of_stay_days": item["length_of_stay_days"],
                    "reason": item["reason"],
                    "status": item["status"],
                    "source": _source(item),
                }
                for item in records.encounters(patient)
            ]
        ),
        "labs": _cut([observation(item) for item in records.observations(patient, category="laboratory")]),
        "vitals": _cut([observation(item) for item in records.observations(patient, category="vital-signs")]),
        "reports": _cut(
            [
                {
                    "name": item["name"],
                    "effective": item["effective"],
                    "conclusion": item["conclusion"],
                    "result_count": len(item["results"]),
                    "source": _source(item),
                }
                for item in records.diagnostic_reports(patient)
            ]
        ),
        "notes": _cut(_metadata(manifest["document_metadata"])),
    }


def record_view(
    alias: str, status: dict[str, Any], runtime: Any | None, manifest: dict[str, Any] | None
) -> dict[str, Any]:
    """The view for one MOCK alias. ``runtime`` and ``manifest`` are None when no verified generation is active."""
    assert alias in MOCK_PATIENTS
    active = status.get("active")
    view: dict[str, Any] = {
        "mode": "MOCK",
        "source": "epic-sandbox-mock",
        "data_origin": "synthetic_mock_transport",
        "alias": alias,
        "label": LABELS[alias],
        "serving": status.get("serving", "unavailable"),
        "active": (
            {key: active.get(key) for key in ("generation_id", "retrieved_at", "promoted_at")} if active else None
        ),
        "attempt": _attempt(status.get("attempt")),
        "evidence_error": status.get("evidence_error"),
        "coverage": None,
        "provenance": None,
        "record": None,
    }
    if runtime is not None and manifest is not None:
        coverage = manifest["coverage"]
        view["coverage"] = {
            "sources": dict(runtime.records.source_coverage),
            "operations": {
                name: {key: counts.get(key) for key in OPERATION_FIELDS}
                for name, counts in sorted(coverage["operations"].items())
            },
            "unresolved_references": coverage.get("unresolved_references", 0),
            "note_bodies": coverage.get("note_bodies", "not_imported"),
            "field_status": (coverage.get("mapping") or {}).get("field_status", {}),
        }
        view["provenance"] = {key: manifest.get(key) for key in PROVENANCE_FIELDS} | {
            "mode": manifest.get("mode"),
            "source": manifest.get("source"),
        }
        view["record"] = _record(runtime, manifest)
    view["warnings"] = _warnings(view, manifest if view["record"] is not None else None)
    return view
