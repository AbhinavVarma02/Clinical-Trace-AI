"""LIVE record projection, internal by default; one explicit offline API opt-in accepts only synthetic evidence.

Read only an approved active LIVE generation through its existing hash/tier verifier. Reuse the MOCK record's
field projection over a fresh in-memory FHIR store, never a clinical runtime, file database, pin or cache. Test-only
synthetic LIVE-shaped evidence is labelled explicitly. Real promotion and genuine owner authentication remain gates.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any, Callable

from api.beta_record import OPERATION_FIELDS, PROVENANCE_FIELDS, _attempt, _cut, _metadata, _record, _warnings
from src.clinical.fhir_models import normalize_timestamp, parse_timestamp, patient_id_of
from src.clinical.records import ClinicalRecords
from src.clinical.store import FHIRStore
from src.interop.epic import settings
from src.interop.epic.beta_acquisition import AcquisitionError, object_json
from src.interop.epic.beta_catalog import RefreshRejected
from src.interop.epic.beta_generations import FRESH_S, GENERATION_ID, Generations
from src.interop.epic.beta_live_generations import LiveGenerations
from src.interop.epic.beta_live_staging import ATTEMPT
from src.interop.epic.beta_staging import CLINICAL_TYPES, _check_source
from src.interop.epic.client import valid_fhir_id
from src.interop.epic.mapping import internal_id
from src.persistence.db import Database
from src.sources import source_provenance


@dataclass(frozen=True)
class LiveEvidenceBinding:
    """Exact evidence authorized outside the browser for promotion or serving."""

    attempt_id: str
    generation_id: str
    manifest_sha256: str
    bundle_sha256: str

    def __post_init__(self) -> None:
        if (
            not ATTEMPT.fullmatch(self.attempt_id)
            or not GENERATION_ID.fullmatch(self.generation_id)
            or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in (self.manifest_sha256, self.bundle_sha256))
        ):
            raise ValueError("exact LIVE evidence binding required")

    def require(self, manifest: dict[str, Any], row: dict[str, Any]) -> dict[str, str]:
        if (
            self.attempt_id != row["attempt_id"]
            or self.generation_id != manifest["generation_id"]
            or self.manifest_sha256 != row["manifest_sha256"]
            or self.bundle_sha256 != manifest["bundle_sha256"]
        ):
            raise RefreshRejected(409, "generation_not_approved")
        return asdict(self)


def _operations(operations: dict[str, Any]) -> dict[str, Any]:
    return {
        name: {key: counts.get(key) for key in (*OPERATION_FIELDS, "informational_codes", "http_statuses")}
        for name, counts in sorted(operations.items())
    }


def _resources(manifest: dict[str, Any], bundle: bytes) -> list[dict[str, Any]]:
    document = object_json(bundle)
    if document["resourceType"] != "Bundle" or document["type"] != "collection":
        raise ValueError
    resources = [entry["resource"] for entry in document["entry"]]
    patient = manifest["patient_id"]
    identities = set()
    for resource in resources:
        kind, rid = resource["resourceType"], resource["id"]
        if kind not in CLINICAL_TYPES or patient_id_of(resource) != patient or (kind, rid) in identities:
            raise ValueError
        _check_source(resource, patient)  # includes nested patient references, not just subject
        identities.add((kind, rid))
    counts = Counter(resource["resourceType"] for resource in resources)
    if counts.get("Patient") != 1 or dict(counts) != manifest["resource_counts"]:
        raise ValueError
    return resources


def _mapped_source(resource: dict[str, Any]) -> dict[str, Any]:
    """Use Epic lineage, which distinguishes the source update from the fallback ingest timestamp."""
    lineage = source_provenance(resource)
    if lineage is None:
        raise ValueError("missing Epic lineage")
    kind, separator, source_id = lineage["source_reference"].partition("/")
    digest = lineage["source_content_sha256"]
    if (
        not separator
        or kind != resource["resourceType"]
        or internal_id(kind, valid_fhir_id(source_id)) != resource["id"]
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise ValueError("invalid Epic lineage")
    timestamp = lineage["source_last_updated"]
    return {
        "version": lineage["source_version_id"],
        "last_updated": normalize_timestamp(timestamp) if timestamp else None,
        "content_sha256": digest,
    }


def _documents(manifest: dict[str, Any], patient: str) -> dict[str, Any]:
    items, identities = [], set()
    for document in manifest["document_metadata"]:
        rid = document["id"]
        valid_fhir_id(rid)
        digest = document.get("_sourceContentSha256")
        if (
            document["resourceType"] != "DocumentReference"
            or rid in identities
            or document["subject"]["reference"] not in {f"Patient/{patient}", f"{settings.FHIR_BASE}/Patient/{patient}"}
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise ValueError
        _check_source(document, patient)
        identities.add(rid)
        meta = document.get("meta") or {}
        item = _metadata([document])[0]
        item["source"] = {
            "reference": f"DocumentReference/{internal_id('DocumentReference', rid)}",
            "version": meta.get("versionId"),
            "last_updated": normalize_timestamp(meta["lastUpdated"]) if meta.get("lastUpdated") else None,
            "synthetic": manifest["synthetic_transport"],
            "content_sha256": digest,
        }
        items.append(item)
    return _cut(
        sorted(items, key=lambda item: (bool(item["date"]), parse_timestamp(item["date"]) if item["date"] else None))
    )


def live_record_view(
    generations: LiveGenerations,
    alias: str,
    *,
    authorized: Callable[[], bool],
    synthetic_only: bool = False,
    genuine_only: bool = False,
    approved: LiveEvidenceBinding | None = None,
) -> dict[str, Any]:
    """Build a preview only. Caller authorization is mandatory; the offline API additionally requires synthetic_only.

    The outer envelope resembles the Beta record response, but mode/origin explicitly identify the LIVE tier and
    synthetic fixtures. Serving and publication stay unavailable/disabled, regardless of the preview's freshness.
    """

    def project(manifest: dict[str, Any], bundle: bytes, row: dict[str, Any]) -> dict[str, Any]:
        if genuine_only and (synthetic_only or manifest.get("synthetic_transport") is not False):
            raise RefreshRejected(409, "genuine_evidence_required")
        if synthetic_only and manifest.get("synthetic_transport") is not True:
            raise RefreshRejected(409, "synthetic_evidence_required")
        if approved is not None:
            approved.require(manifest, row)
        try:
            resources = _resources(manifest, bundle)
            patient = generations.allowlist.get(alias)
            if patient is None:
                raise ValueError
            notes = _documents(manifest, patient)
            database = Database(":memory:")  # explicit path: never consult the ambient default database
            try:
                store = FHIRStore(database)
                report = store.ingest(resources, source=manifest["source"])
                if (
                    report.invalid
                    or report.inserted != len(resources)
                    or store.patient_ids() != [manifest["patient_id"]]
                ):
                    raise ValueError
                records = ClinicalRecords(store)
                record = _record(SimpleNamespace(store=store, records=records), manifest)
                sources = {
                    f"{resource['resourceType']}/{resource['id']}": _mapped_source(resource) for resource in resources
                }
                record["patient"]["source"].update(sources[record["patient"]["source"]["reference"]])
                for section in ("conditions", "encounters", "labs", "vitals", "reports"):
                    for item in record[section]["items"]:
                        item["source"].update(sources[item["source"]["reference"]])
                record["notes"] = notes
            finally:
                database.close()

            status = generations.catalog.status(alias, live=True)
            attempt = _attempt(status["attempt"])
            if attempt is not None:
                attempt["operations"] = _operations(status["attempt"]["operations"])
            age = generations.catalog.clock() - manifest["retrieved_at"]
            latest = status["attempt"]
            freshness = "stale" if age > FRESH_S else "cached"
            if age <= FRESH_S and latest is not None and latest["attempt_id"] == row["attempt_id"]:
                freshness = "fresh"
            active = {
                "generation_id": row["generation_id"],
                "retrieved_at": row["retrieved_at"],
                "promoted_at": row["approved_at"],
            }
            coverage = manifest["coverage"]
            warnings = [
                warning
                for warning in _warnings({"active": active, "serving": freshness, "attempt": attempt}, manifest)
                if warning["code"] != "synthetic_mock"
            ]
            warnings[:0] = [
                {
                    "code": "live_access_disabled",
                    "severity": "warning",
                    "message": "Real LIVE browser/API serving and clinical AI remain disabled.",
                },
                {
                    "code": "completeness_unconfirmed",
                    "severity": "warning",
                    "message": "Epic retrieval completeness is unconfirmed; absence here is not evidence of absence.",
                },
            ]
            if manifest["synthetic_transport"]:
                warnings.insert(
                    0,
                    {
                        "code": "synthetic_live_fixture",
                        "severity": "info",
                        "message": "Test-only synthetic LIVE-shaped evidence. This is not an Epic-acquired patient record.",
                    },
                )
            if any(counts.get("informational_codes") for counts in coverage["operations"].values()):
                warnings.append(
                    {
                        "code": "upstream_information",
                        "severity": "warning",
                        "message": "Search outcomes include informational codes; see per-operation coverage. Completeness is unconfirmed.",
                    }
                )
            return {
                "mode": "LIVE",
                "source": manifest["source"],
                "data_origin": "synthetic_live_fixture" if manifest["synthetic_transport"] else "epic_sandbox",
                "alias": alias,
                "label": "Synthetic LIVE-shaped fixture"
                if manifest["synthetic_transport"]
                else "Approved LIVE patient",
                "serving": "unavailable",
                "publication": "disabled",
                "freshness": freshness,
                "completeness": manifest["completeness"],
                "active": active,
                "attempt": attempt,
                "evidence_error": None,
                "coverage": {
                    "sources": Generations.source_coverage(manifest),
                    "operations": _operations(coverage["operations"]),
                    "unresolved_references": coverage["unresolved_references"],
                    "note_bodies": coverage["note_bodies"],
                    "field_status": coverage["mapping"]["field_status"],
                    "completeness": manifest["completeness"],
                },
                "provenance": {key: manifest.get(key) for key in PROVENANCE_FIELDS}
                | {
                    "mode": manifest["mode"],
                    "source": manifest["source"],
                    "manifest_sha256": row["manifest_sha256"],
                    "attempt_id": row["attempt_id"],
                    "completeness": manifest["completeness"],
                    "synthetic_transport": manifest["synthetic_transport"],
                },
                "record": record,
                "warnings": warnings,
            }
        except (AcquisitionError, OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.DatabaseError):
            raise RefreshRejected(409, "generation_integrity") from None

    return generations.project_active(alias, authorized, project)
