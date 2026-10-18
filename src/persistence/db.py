"""SQLite database access and forward-only schema migrations.

SQLite is the default (zero-cost, file-based, works on Windows/WSL2/Docker).
The schema is written in portable SQL so it can move to PostgreSQL later; the
pgvector path is documented in ``docs/ADR/0003-persistence.md`` and not yet
implemented.

The database path comes from ``CLINICAL_TRACE_DB_PATH`` (default
``var/clinical_trace.db``). ``:memory:`` is supported for tests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import PROJECT_ROOT

DEFAULT_DB_PATH = PROJECT_ROOT / "var" / "clinical_trace.db"

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "fhir_resources", """
CREATE TABLE ingestion_run (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    received INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    invalid INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE fhir_resource (
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    version_id INTEGER NOT NULL,
    patient_id TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    source TEXT NOT NULL,
    ingestion_run_id TEXT NOT NULL REFERENCES ingestion_run(run_id),
    ingested_at TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (resource_type, resource_id, version_id)
);
CREATE INDEX ix_fhir_patient_type ON fhir_resource(patient_id, resource_type, is_current);
CREATE TABLE ingestion_issue (
    run_id TEXT NOT NULL REFERENCES ingestion_run(run_id),
    resource_ref TEXT NOT NULL,
    issue TEXT NOT NULL
);
"""),
    (2, "agent_sessions_runs", """
CREATE TABLE agent_session (
    session_id TEXT PRIMARY KEY,
    persona TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE agent_run (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_session(session_id),
    persona TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    inference_mode TEXT NOT NULL,
    result TEXT,
    trace TEXT,
    latency_ms REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_agent_run_session ON agent_run(session_id, created_at);
"""),
    (3, "governance", """
CREATE TABLE artifact (
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    run_id TEXT,
    patient_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, version)
);
CREATE TABLE review (
    review_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    artifact_version INTEGER NOT NULL,
    artifact_hash TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE review_event (
    event_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES review(review_id),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE audit_event (
    event_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    persona TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    request_id TEXT
);
CREATE INDEX ix_audit_ts ON audit_event(ts);
CREATE TABLE feedback (
    feedback_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    persona TEXT NOT NULL,
    category TEXT NOT NULL,
    rating INTEGER,
    comment TEXT,
    created_at TEXT NOT NULL
);
"""),
    (4, "request_log", """
CREATE TABLE request_log (
    request_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    method TEXT NOT NULL,
    route TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    latency_ms REAL NOT NULL
);
CREATE INDEX ix_request_log_ts ON request_log(ts);
"""),
    (5, "durable_agent_controls", """
ALTER TABLE agent_run ADD COLUMN idempotency_key TEXT;
ALTER TABLE agent_run ADD COLUMN engine_revision TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_run ADD COLUMN data_version TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_run ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_run ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX ix_agent_idempotency ON agent_run(persona, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""),
    (6, "index_evaluation_metadata", """
CREATE TABLE retrieval_manifest (
    index_version TEXT PRIMARY KEY,
    embedder TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE retrieval_metadata (
    index_version TEXT NOT NULL REFERENCES retrieval_manifest(index_version),
    chunk_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (index_version, chunk_id)
);
CREATE TABLE evaluation_run (
    evaluation_id TEXT PRIMARY KEY,
    run_at TEXT NOT NULL,
    source_commit TEXT,
    dataset_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    gate_passed INTEGER NOT NULL,
    metadata TEXT NOT NULL
);
"""),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str | Path | None = None) -> None:
        raw = str(path) if path is not None else os.getenv("CLINICAL_TRACE_DB_PATH", str(DEFAULT_DB_PATH))
        self.path = raw
        if raw != ":memory:":
            Path(raw).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(raw, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if raw != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def migrate(self) -> list[int]:
        """Apply pending migrations in order; return the versions applied."""
        applied: list[int] = []
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            done = {row[0] for row in self._conn.execute("SELECT version FROM schema_migrations")}
            for version, name, sql in MIGRATIONS:
                if version in done:
                    continue
                self._conn.execute("BEGIN")
                try:
                    for statement in [s.strip() for s in sql.split(";") if s.strip()]:
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, utc_now()),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                applied.append(version)
        return applied

    def schema_version(self) -> int:
        row = self.one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"] or 0) if row else 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
