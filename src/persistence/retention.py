"""Explicit local retention, dry-run by default, with review and active-run holds."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.persistence.db import Database


def prune(db: Database, run_days: int = 30, log_days: int = 7, audit_days: int = 90,
          apply: bool = False, now: datetime | None = None) -> dict[str, Any]:
    if min(run_days, log_days, audit_days) < 1:
        raise ValueError("retention windows must be at least one day")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("retention time must have an explicit timezone")

    def cutoff(days: int) -> str:
        return (instant - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # Selection/deletion share one transaction: new reviews/active status cannot race pruning.
    with db.transaction() as conn:
        runs = [r[0] for r in conn.execute(
            "SELECT run_id FROM agent_run WHERE created_at<? AND status IN ('completed','failed','cancelled') "
            "AND NOT EXISTS (SELECT 1 FROM artifact WHERE artifact.run_id=agent_run.run_id)", (cutoff(run_days),))]
        logs = conn.execute("SELECT COUNT(*) FROM request_log WHERE ts<?", (cutoff(log_days),)).fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit_event WHERE ts<?", (cutoff(audit_days),)).fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checkpoints = 0
        for run_id in runs:
            if "checkpoints" in tables:
                checkpoints += conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?", (run_id,)).fetchone()[0]
            if apply:
                if "writes" in tables:
                    conn.execute("DELETE FROM writes WHERE thread_id=?", (run_id,))
                if "checkpoints" in tables:
                    conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (run_id,))
                conn.execute("DELETE FROM feedback WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM agent_run WHERE run_id=?", (run_id,))
        if apply:
            conn.execute("DELETE FROM agent_session WHERE NOT EXISTS "
                         "(SELECT 1 FROM agent_run WHERE agent_run.session_id=agent_session.session_id) AND updated_at<?",
                         (cutoff(run_days),))
            conn.execute("DELETE FROM request_log WHERE ts<?", (cutoff(log_days),))
            conn.execute("DELETE FROM audit_event WHERE ts<?", (cutoff(audit_days),))
    return {"applied": apply, "runs": len(runs), "checkpoints": checkpoints, "request_logs": logs,
            "audit_events": audits, "policy_days": {"runs": run_days, "request_logs": log_days, "audit": audit_days},
            "holds": "All active/interrupted runs and every run with a review artifact are retained. Clinical resources, reviews and evaluation artifacts are retained."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-days", type=int, default=30)
    parser.add_argument("--log-days", type=int, default=7)
    parser.add_argument("--audit-days", type=int, default=90)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("database must already exist")
    db = Database(args.database)
    try:
        print(json.dumps(prune(db, args.run_days, args.log_days, args.audit_days, args.apply)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
