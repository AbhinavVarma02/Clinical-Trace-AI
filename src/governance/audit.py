"""Persistent audit log for authorisation decisions and governance actions.

Audit rows hold identifiers only (persona, action, resource reference,
decision, reason) - never record text or question content.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.persistence.db import Database, utc_now
from src.safety.policy import AccessContext, Decision, display_principal, scope_filter


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, context: AccessContext | None, action: str, resource: str, decision: Decision) -> None:
        persona = context.principal if context else "anonymous"
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

    def recent(self, limit: int = 100, decision: str | None = None,
               context: AccessContext | None = None) -> list[dict[str, Any]]:
        """Recent events; with a workspace-scoped ``context`` only that workspace's events are returned."""
        clauses, params = [], []
        if context is not None:
            clause, scope = scope_filter(context, "persona")
            clauses.append(clause)
            params.extend(scope)
        if decision:
            clauses.append("decision=?")
            params.append(decision)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self.db.all(f"SELECT * FROM audit_event {where}ORDER BY ts DESC LIMIT ?", (*params, limit))
        return [{**dict(row), "persona": display_principal(row["persona"])} for row in rows]

    def counts(self, context: AccessContext | None = None) -> dict[str, int]:
        clause, params = scope_filter(context, "persona") if context is not None else ("1=1", ())
        rows = self.db.all(f"SELECT decision, COUNT(*) AS n FROM audit_event WHERE {clause} GROUP BY decision", params)
        return {row["decision"]: int(row["n"]) for row in rows}
