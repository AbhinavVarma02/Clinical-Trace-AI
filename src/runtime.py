"""Application runtime: builds and holds the shared services (one per process).

Startup is idempotent: the synthetic FHIR fixture is ingested on every start,
and unchanged resources are recorded as duplicates rather than re-inserted.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.agent.llm import OpenAICompatibleProvider, provider_from_env
from src.agent.service import AgentService
from src.clinical.records import ClinicalRecords
from src.clinical.store import FHIRStore
from src.clinical.synthetic import FIXTURE_PATH
from src.governance.audit import AuditLog
from src.governance.review import ReviewService
from src.persistence.db import Database
from src.retrieval.index import RetrievalIndex
from src.safety.guards import flag_content
from src.safety.policy import PolicyEngine


@dataclass
class Runtime:
    db: Database
    store: FHIRStore
    records: ClinicalRecords
    index: RetrievalIndex
    audit: AuditLog
    policy: PolicyEngine
    agent: AgentService
    reviews: ReviewService
    provider: OpenAICompatibleProvider | None
    model_available: bool
    started_at: float
    startup_ingestion: dict[str, Any]

    def close(self, grace_s: float = 10.0) -> bool:
        if not self.agent.close(grace_s):
            return False
        if self.provider:
            self.provider.close()
        self.db.close()
        return True


def _predictor() -> tuple[Callable[[dict[str, Any]], dict[str, Any]] | None, bool]:
    try:
        from src.predict import is_model_loaded, predict
    except Exception:  # pragma: no cover - optional ML stack
        return None, False
    if not is_model_loaded():
        return None, False
    return predict, True


def build_runtime(
    db_path: str | Path | None = None,
    fixture: Path = FIXTURE_PATH,
    provider: OpenAICompatibleProvider | None | str = "env",
    enable_model: bool = True,
) -> Runtime:
    db = Database(db_path)
    # One application worker per SQLite database. Never auto-replay clinical work.
    db.execute("UPDATE agent_run SET status='interrupted' WHERE status='running'")
    store = FHIRStore(db)
    records = ClinicalRecords(store)
    index = RetrievalIndex(records, flagger=flag_content)
    store.on_change(index.refresh)
    report = store.ingest_bundle_file(fixture)
    if not index.built_at:
        index.build()
    audit = AuditLog(db)
    policy = PolicyEngine(audit_sink=audit.record)
    chosen = provider_from_env() if provider == "env" else provider
    predictor, model_available = _predictor() if enable_model else (None, False)
    agent = AgentService(db, records, index, policy, provider=chosen, predictor=predictor)  # type: ignore[arg-type]
    return Runtime(
        db=db, store=store, records=records, index=index, audit=audit, policy=policy, agent=agent,
        reviews=ReviewService(db, policy, audit), provider=chosen,  # type: ignore[arg-type]
        model_available=model_available, started_at=time.time(),
        startup_ingestion={k: v for k, v in report.as_dict().items() if k != "issues"},
    )


_runtime: Runtime | None = None
_lock = threading.Lock()


def get_runtime() -> Runtime:
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = build_runtime(os.getenv("CLINICAL_TRACE_DB_PATH") or None)
        return _runtime


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    with _lock:
        _runtime = runtime


def close_runtime() -> bool:
    global _runtime
    with _lock:
        if _runtime is None:
            return True
        if not _runtime.close():
            return False
        _runtime = None
        return True
