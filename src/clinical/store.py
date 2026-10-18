"""Versioned FHIR resource store with idempotent, lineage-tracked ingestion.

Every resource version is kept. Re-ingesting identical content is a no-op
(counted as a duplicate); a *different* payload under an existing version id is
rejected as a conflict rather than silently overwritten. Each stored row points
at the ``ingestion_run`` that produced it, so provenance can be reconstructed.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.clinical.fhir_models import (
    normalize_timestamp,
    patient_id_of,
    validate_resource,
)
from src.persistence.db import Database, dumps, utc_now


def content_hash(resource: dict[str, Any]) -> str:
    return hashlib.sha256(dumps(resource).encode("utf-8")).hexdigest()


def _ingest_order(resource: Any) -> tuple[str, str, int]:
    if not isinstance(resource, dict):
        return ("", "", 0)
    version = str((resource.get("meta") or {}).get("versionId", "0"))
    return (str(resource.get("resourceType")), str(resource.get("id")), int(version) if version.isdigit() else 0)


@dataclass
class IngestionReport:
    run_id: str
    source: str
    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    invalid: int = 0
    issues: list[dict[str, str]] = field(default_factory=list)
    status: str = "running"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "received": self.received,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "invalid": self.invalid,
            "issues": self.issues,
            "status": self.status,
        }


class FHIRStore:
    """Read/write access to stored FHIR resources (write = ingestion only)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._listeners: list[Any] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ ingest
    def on_change(self, callback: Any) -> None:
        """Register a callback(patient_ids: set[str]) fired after ingestion."""
        self._listeners.append(callback)

    def ingest(self, resources: Iterable[dict[str, Any]], source: str) -> IngestionReport:
        report = IngestionReport(run_id=str(uuid.uuid4()), source=source)
        changed_patients: set[str] = set()
        with self._lock:
            self.db.execute(
                "INSERT INTO ingestion_run(run_id, source, started_at, status) VALUES (?, ?, ?, 'running')",
                (report.run_id, source, utc_now()),
            )
            try:
                # Sort so older versions are written before newer ones.
                ordered = sorted(resources, key=_ingest_order)
                for resource in ordered:
                    report.received += 1
                    outcome = self._ingest_one(resource, report)
                    if outcome == "inserted":
                        changed_patients.add(str(patient_id_of(resource)))
                report.status = "succeeded"
            except Exception as exc:  # pragma: no cover - defensive
                report.status = "failed"
                self.db.execute(
                    "UPDATE ingestion_run SET status='failed', error=?, finished_at=? WHERE run_id=?",
                    (type(exc).__name__, utc_now(), report.run_id),
                )
                raise
            self.db.execute(
                "UPDATE ingestion_run SET finished_at=?, received=?, inserted=?, duplicates=?, "
                "invalid=?, status=? WHERE run_id=?",
                (utc_now(), report.received, report.inserted, report.duplicates, report.invalid,
                 report.status, report.run_id),
            )
        if changed_patients:
            for callback in self._listeners:
                callback(changed_patients)
        return report

    def _ingest_one(self, resource: dict[str, Any], report: IngestionReport) -> str:
        ref = f"{resource.get('resourceType')}/{resource.get('id')}" if isinstance(resource, dict) else "?"
        issues = validate_resource(resource)
        if issues:
            report.invalid += 1
            for issue in issues:
                report.issues.append({"resource": ref, "issue": issue})
                self.db.execute(
                    "INSERT INTO ingestion_issue(run_id, resource_ref, issue) VALUES (?, ?, ?)",
                    (report.run_id, ref, issue),
                )
            return "invalid"
        rtype, rid = resource["resourceType"], resource["id"]
        version = int(resource["meta"]["versionId"])
        digest = content_hash(resource)
        existing = self.db.one(
            "SELECT content_hash FROM fhir_resource WHERE resource_type=? AND resource_id=? AND version_id=?",
            (rtype, rid, version),
        )
        if existing is not None:
            if existing["content_hash"] == digest:
                report.duplicates += 1
                return "duplicate"
            report.invalid += 1
            issue = f"version {version} already stored with different content (conflict)"
            report.issues.append({"resource": ref, "issue": issue})
            self.db.execute(
                "INSERT INTO ingestion_issue(run_id, resource_ref, issue) VALUES (?, ?, ?)",
                (report.run_id, ref, issue),
            )
            return "conflict"
        latest = self.db.one(
            "SELECT MAX(version_id) AS v FROM fhir_resource WHERE resource_type=? AND resource_id=?",
            (rtype, rid),
        )
        latest_version = int(latest["v"]) if latest and latest["v"] is not None else 0
        is_current = version > latest_version
        with self.db.transaction() as conn:
            if is_current:
                conn.execute(
                    "UPDATE fhir_resource SET is_current=0 WHERE resource_type=? AND resource_id=?",
                    (rtype, rid),
                )
            conn.execute(
                "INSERT INTO fhir_resource(resource_type, resource_id, version_id, patient_id, last_updated, "
                "content_hash, payload, source, ingestion_run_id, ingested_at, is_current) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rtype, rid, version, patient_id_of(resource),
                    normalize_timestamp(resource["meta"]["lastUpdated"]), digest, dumps(resource),
                    str(resource["meta"].get("source", report.source)), report.run_id, utc_now(),
                    1 if is_current else 0,
                ),
            )
        report.inserted += 1
        return "inserted"

    def ingest_bundle_file(self, path: Path) -> IngestionReport:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        resources = [entry["resource"] for entry in bundle.get("entry", []) if "resource" in entry]
        return self.ingest(resources, source=f"file:{path.name}")

    # -------------------------------------------------------------------- read
    def patient_ids(self) -> list[str]:
        rows = self.db.all(
            "SELECT DISTINCT resource_id FROM fhir_resource WHERE resource_type='Patient' AND is_current=1 "
            "ORDER BY resource_id"
        )
        return [row["resource_id"] for row in rows]

    def get(self, resource_type: str, resource_id: str, version: int | None = None) -> dict[str, Any] | None:
        if version is None:
            row = self.db.one(
                "SELECT payload FROM fhir_resource WHERE resource_type=? AND resource_id=? AND is_current=1",
                (resource_type, resource_id),
            )
        else:
            row = self.db.one(
                "SELECT payload FROM fhir_resource WHERE resource_type=? AND resource_id=? AND version_id=?",
                (resource_type, resource_id, version),
            )
        return json.loads(row["payload"]) if row else None

    def history(self, resource_type: str, resource_id: str) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT payload FROM fhir_resource WHERE resource_type=? AND resource_id=? ORDER BY version_id",
            (resource_type, resource_id),
        )
        return [json.loads(row["payload"]) for row in rows]

    def search(
        self,
        patient_id: str,
        resource_type: str,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Patient-scoped search over current versions. Returns (page, total)."""
        total_row = self.db.one(
            "SELECT COUNT(*) AS n FROM fhir_resource WHERE patient_id=? AND resource_type=? AND is_current=1",
            (patient_id, resource_type),
        )
        rows = self.db.all(
            "SELECT payload FROM fhir_resource WHERE patient_id=? AND resource_type=? AND is_current=1 "
            "ORDER BY resource_id LIMIT ? OFFSET ?",
            (patient_id, resource_type, limit, offset),
        )
        return [json.loads(row["payload"]) for row in rows], int(total_row["n"] if total_row else 0)

    def all_for_patient(self, patient_id: str, resource_type: str) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT payload FROM fhir_resource WHERE patient_id=? AND resource_type=? AND is_current=1 "
            "ORDER BY resource_id",
            (patient_id, resource_type),
        )
        return [json.loads(row["payload"]) for row in rows]

    def provenance(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT r.version_id, r.last_updated, r.content_hash, r.source, r.ingested_at, r.ingestion_run_id, "
            "i.source AS run_source FROM fhir_resource r JOIN ingestion_run i ON i.run_id = r.ingestion_run_id "
            "WHERE r.resource_type=? AND r.resource_id=? AND r.is_current=1",
            (resource_type, resource_id),
        )
        if not row:
            return None
        return {
            "reference": f"{resource_type}/{resource_id}",
            "version_id": row["version_id"],
            "last_updated": row["last_updated"],
            "content_hash": row["content_hash"],
            "generator_source": row["source"],
            "ingested_at": row["ingested_at"],
            "ingestion_run_id": row["ingestion_run_id"],
            "ingestion_source": row["run_source"],
        }

    def freshness(self, patient_id: str) -> dict[str, Any]:
        row = self.db.one(
            "SELECT MAX(last_updated) AS lu, MAX(ingested_at) AS ia, COUNT(*) AS n FROM fhir_resource "
            "WHERE patient_id=? AND is_current=1",
            (patient_id,),
        )
        return {
            "latest_record_update": row["lu"] if row else None,
            "last_ingested_at": row["ia"] if row else None,
            "current_resource_count": int(row["n"]) if row else 0,
        }

    def ingestion_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM ingestion_run ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]
