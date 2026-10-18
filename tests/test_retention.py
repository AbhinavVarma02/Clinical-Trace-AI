from datetime import datetime, timezone

from src.persistence.retention import prune
from src.safety.policy import PERSONAS, AccessContext


def test_retention_dry_run_active_and_review_holds(runtime):
    access = AccessContext(PERSONAS["clinician.demo"])
    old = "2000-01-01T00:00:00.000Z"
    runs = [runtime.agent.run(access, "synthetic_p001", "Why was metformin stopped?") for _ in range(3)]
    runtime.reviews.submit_run(access, runs[1])
    runtime.db.execute("UPDATE agent_run SET created_at=?", (old,))
    runtime.db.execute("UPDATE agent_session SET updated_at=?", (old,))
    runtime.db.execute("UPDATE agent_run SET status='interrupted' WHERE run_id=?", (runs[2]["run_id"],))
    runtime.db.execute("INSERT INTO request_log VALUES (?,?,?,?,?,?)", ("old", old, "GET", "/test", 200, 1.0))
    runtime.db.execute("UPDATE audit_event SET ts=?", (old,))
    args = {"now": datetime(2026, 10, 17, tzinfo=timezone.utc)}
    # In-memory app and checkpoint DBs are separate; file-based pruning tested below.
    dry = prune(runtime.db, **args)
    assert dry["runs"] == 1 and not dry["applied"] and dry["request_logs"] == 1
    assert runtime.db.one("SELECT COUNT(*) AS n FROM agent_run")["n"] == 3
    applied = prune(runtime.db, apply=True, **args)
    assert applied["runs"] == 1 and applied["audit_events"] > 0
    assert runtime.db.one("SELECT COUNT(*) AS n FROM agent_run")["n"] == 2
    assert runtime.db.one("SELECT COUNT(*) AS n FROM artifact")["n"] == 1


def test_retention_deletes_file_backed_checkpoints_atomically(tmp_path):
    from src.runtime import build_runtime

    rt = build_runtime(tmp_path / "retention.db", provider=None, enable_model=False)
    try:
        rt.agent.run(AccessContext(PERSONAS["clinician.demo"]), "synthetic_p001", "HbA1c trend")
        rt.db.execute("UPDATE agent_run SET created_at='2000-01-01T00:00:00.000Z'")
        dry = prune(rt.db)
        assert dry["checkpoints"] > 0
        prune(rt.db, apply=True)
        assert rt.db.one("SELECT COUNT(*) AS n FROM checkpoints")["n"] == 0
        assert rt.db.one("SELECT COUNT(*) AS n FROM writes")["n"] == 0
    finally:
        rt.close()
