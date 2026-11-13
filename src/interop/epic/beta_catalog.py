"""Durable, content-free acquisition control catalog; browser refresh remains MOCK-only."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

MOCK_PATIENTS = MappingProxyType({"mock-alpha": "invented-patient-alpha", "mock-bravo": "invented-patient-bravo"})
POLICY_VERSION = "mock-initial-r4-v1"
LIVE_POLICY_VERSION = "live-initial-r4-v1"
HOUR = 3600


class RefreshRejected(Exception):
    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        self.status, self.code, self.retry_after = status, code, retry_after
        super().__init__(code)


class Catalog:
    """All methods run on the app loop, without awaiting inside a transaction."""

    def __init__(
        self, path: Path, clock: Callable[[], float], *, initialize: bool = True, readonly: bool = False
    ) -> None:
        self.clock = clock
        if initialize and readonly:
            raise ValueError("initializing a catalog requires write access")
        self.db = (
            sqlite3.connect(path, timeout=0.1)
            if initialize
            else sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=0.1)
        )
        self.db.row_factory = sqlite3.Row
        try:
            # Keep the file exclusively locked for this app's lifetime. A competing
            # worker must fail startup instead of recovering a worker that's still live.
            self.db.execute("PRAGMA locking_mode=EXCLUSIVE")
            if not initialize:
                # Reopening a selected scope must not perform DDL, recover abandoned attempts or create a file.
                self.db.execute("SELECT 1 FROM attempt LIMIT 1").fetchone()
                return
            self.db.executescript(
                """
                BEGIN EXCLUSIVE;
                CREATE TABLE IF NOT EXISTS attempt (
                    attempt_id TEXT PRIMARY KEY, key_digest TEXT UNIQUE NOT NULL, alias TEXT NOT NULL,
                    state TEXT NOT NULL, policy TEXT NOT NULL, admitted_at REAL NOT NULL,
                    started_at REAL, finished_at REAL, error_code TEXT, operations TEXT
                );
                CREATE INDEX IF NOT EXISTS attempt_window ON attempt (admitted_at);
                CREATE INDEX IF NOT EXISTS attempt_patient ON attempt (alias, admitted_at);
                CREATE TABLE IF NOT EXISTS dispatch (attempt_id TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS dispatch_window ON dispatch (at);
                CREATE INDEX IF NOT EXISTS dispatch_attempt ON dispatch (attempt_id);
                CREATE TABLE IF NOT EXISTS revoked (target TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY, at REAL NOT NULL, code TEXT NOT NULL, attempt_id TEXT
                );
                CREATE TABLE IF NOT EXISTS patient_head (
                    alias TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                    active TEXT, last_success TEXT, rolled_back INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS attempt_fence (
                    attempt_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, revision INTEGER NOT NULL
                );
                COMMIT;
                """
            )
            with self.db:
                abandoned = self.db.execute(
                    "SELECT attempt_id FROM attempt WHERE state IN ('queued','running')"
                ).fetchall()
                for row in abandoned:
                    self.finish(row["attempt_id"], "interrupted", "restart")
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def _audit(self, code: str, attempt: str | None = None) -> None:
        row = self.db.execute("INSERT INTO audit(at, code, attempt_id) VALUES (?, ?, ?)", (self.clock(), code, attempt))
        self.db.execute("DELETE FROM audit WHERE sequence <= ?", ((row.lastrowid or 0) - 4096,))

    def audit(self, code: str, attempt: str | None = None) -> None:
        with self.db:
            self._audit(code, attempt)

    def revoked(self, target: str) -> bool:
        return self.db.execute("SELECT 1 FROM revoked WHERE target = ?", (target,)).fetchone() is not None

    def revoke(self, target: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO revoked VALUES (?)", (target,))
            self.db.execute(
                "UPDATE patient_head SET revision = revision + 1, epoch = epoch + 1 WHERE alias = ? OR ? = 'owner'",
                (target, target),
            )
            self._audit("owner_revoked" if target == "owner" else "patient_revoked")

    def _window(self, now: float, table: str, column: str, alias: str | None = None) -> list[float]:
        # Callers use only fixed schema identifiers.
        sql = f"SELECT {column} FROM {table} WHERE {column} > ?"
        params: tuple[Any, ...] = (now - HOUR,)
        if alias is not None:
            sql += " AND alias = ?"
            params += (alias,)
        return [row[0] for row in self.db.execute(sql + f" ORDER BY {column}", params)]

    def existing_attempt(self, alias: str, key: str) -> str | None:
        key_digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        existing = self.db.execute(
            "SELECT attempt_id, alias FROM attempt WHERE key_digest = ?", (key_digest,)
        ).fetchone()
        if existing is not None:
            if existing["alias"] != alias:
                raise RefreshRejected(409, "idempotency_conflict")
            return str(existing["attempt_id"])
        return None

    def admit(
        self,
        alias: str,
        key: str,
        *,
        cooldown_s: int,
        patient_limit: int,
        global_limit: int,
        request_limit: int,
        policy: str = POLICY_VERSION,
    ) -> tuple[str, bool]:
        if policy not in {POLICY_VERSION, LIVE_POLICY_VERSION}:
            raise RefreshRejected(400, "policy_scope")
        existing = self.existing_attempt(alias, key)
        if existing is not None:
            return existing, False
        key_digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        now = self.clock()
        if self.db.execute("SELECT 1 FROM attempt WHERE state IN ('queued','running')").fetchone():
            raise RefreshRejected(409, "refresh_busy")
        patient = self._window(now, "attempt", "admitted_at", alias)
        total = self._window(now, "attempt", "admitted_at")
        outbound = self._window(now, "dispatch", "at")
        resumes = [now]
        if patient:
            resumes.append(patient[-1] + cooldown_s)
        for items, limit in ((patient, patient_limit), (total, global_limit), (outbound, request_limit)):
            if len(items) >= limit:
                resumes.append(items[-limit] + HOUR)
        if max(resumes) > now:
            raise RefreshRejected(429, "quota_exhausted", math.ceil(max(resumes) - now))
        attempt = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO patient_head(alias) VALUES (?)", (alias,))
            self.db.execute(
                "INSERT INTO attempt (attempt_id, key_digest, alias, state, policy, admitted_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (attempt, key_digest, alias, policy, now),
            )
            self.db.execute(
                "INSERT INTO attempt_fence SELECT ?, epoch, revision FROM patient_head WHERE alias = ?",
                (attempt, alias),
            )
            self._audit("admitted", attempt)
        return attempt, True

    def start(self, attempt: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE attempt SET state = 'running', started_at = ? WHERE attempt_id = ? AND state = 'queued'",
                (self.clock(), attempt),
            )

    def charge(self, attempt: str, attempt_limit: int, global_limit: int) -> None:
        count = self.db.execute("SELECT COUNT(*) FROM dispatch WHERE attempt_id = ?", (attempt,)).fetchone()[0]
        if count >= attempt_limit:
            raise RefreshRejected(429, "request_limit")
        now = self.clock()
        if len(self._window(now, "dispatch", "at")) >= global_limit:
            raise RefreshRejected(429, "outbound_quota")
        with self.db:
            self.db.execute("INSERT INTO dispatch VALUES (?, ?)", (attempt, now))

    def finish(
        self, attempt: str, state: str, code: str | None = None, operations: dict[str, Any] | None = None
    ) -> None:
        with self.db:
            result = self.db.execute(
                "UPDATE attempt SET state = ?, error_code = ?, finished_at = ?, operations = ? "
                "WHERE attempt_id = ? AND state IN ('queued','running')",
                (state, code, self.clock(), json.dumps(operations) if operations is not None else None, attempt),
            )
            if result.rowcount:
                self._audit(code or state, attempt)

    def status(self, alias: str, *, live: bool = False) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT attempt_id, state, policy, admitted_at, started_at, finished_at, error_code, operations, "
            "(SELECT COUNT(*) FROM dispatch WHERE dispatch.attempt_id = attempt.attempt_id) AS requests "
            "FROM attempt WHERE alias = ? ORDER BY admitted_at DESC, rowid DESC LIMIT 1",
            (alias,),
        ).fetchone()
        attempt = dict(row) if row else None
        if attempt is not None:
            attempt["operations"] = json.loads(attempt["operations"]) if attempt["operations"] else {}
            attempt["note_bodies"] = "not_imported"
            attempt["note_body_reason"] = "outside_initial_policy"
        if live:
            # LIVE staging only: no generation exists, so nothing is ever served and publication stays denied.
            return {
                "mode": "LIVE",
                "source": "epic-fhir-sandbox-r4",
                "serving": "unavailable",
                "publication": "disabled",
                "attempt": attempt,
            }
        return {"mode": "MOCK", "source": "epic-sandbox-mock", "serving": "unavailable", "attempt": attempt}
