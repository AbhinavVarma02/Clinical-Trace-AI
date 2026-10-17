"""Durable evaluation provenance, excluding case excerpts and source text."""

from __future__ import annotations

import hashlib
from typing import Any

from src.persistence.db import Database, dumps


def record_evaluation(db: Database, report: dict[str, Any]) -> str:
    payload = {
        k: report.get(k)
        for k in (
            "run_at",
            "git_commit",
            "dataset_version",
            "mode",
            "environment",
            "provider",
            "scoring",
            "summary",
            "retrieval",
            "gate",
        )
    }
    # Retrieval per-query diagnostics may contain query text; retain aggregate metrics only.
    retrieval = report.get("retrieval") or {}
    payload["retrieval"] = {k: retrieval.get(k) for k in ("modes", "isolation")}
    serialized = dumps(payload)
    evaluation_id = hashlib.sha256(serialized.encode()).hexdigest()
    db.execute(
        "INSERT OR IGNORE INTO evaluation_run VALUES (?,?,?,?,?,?,?)",
        (
            evaluation_id,
            report["run_at"],
            report.get("git_commit"),
            report["dataset_version"],
            report["mode"],
            int(report["gate"]["passed"]),
            serialized,
        ),
    )
    return evaluation_id
