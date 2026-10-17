"""Persistent audit log for authorisation decisions and governance actions.

Audit rows hold identifiers only (persona, action, resource reference,
decision, reason) - never record text or question content.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.persistence.db import Database, utc_now
from src.safety.policy import AccessContext, Decision


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, context: AccessContext | None, action: str, resource: str, decision: Decision) -> None:
        persona = context.persona.persona_id if context else "anonymous"
        role = context.persona.role if context else "none"
        self.db.execute(
            "INSERT INTO audit_event(event_id, ts, persona, role, action, resource, decision, reason, request_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), utc_now(), persona, role, action, resource,
             "allow" if decision.allowed else "deny", decision.reason[:300],
             context.request_id if context else None),
        )

    def event(self, context: AccessContext, action: str, resource: str, reason: str) -> None:
        self.record(context, action, resource, Decision(True, reason))

    def recent(self, limit: int = 100, decision: str | None = None) -> list[dict[str, Any]]:
        if decision:
            rows = self.db.all("SELECT * FROM audit_event WHERE decision=? ORDER BY ts DESC LIMIT ?",
                               (decision, limit))
        else:
            rows = self.db.all("SELECT * FROM audit_event ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.db.all("SELECT decision, COUNT(*) AS n FROM audit_event GROUP BY decision")
        return {row["decision"]: int(row["n"]) for row in rows}
