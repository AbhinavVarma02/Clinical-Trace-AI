"""Offline regression gate: the deterministic evaluation must pass its pre-defined thresholds."""

from __future__ import annotations

from evaluation.run_eval import run


def test_deterministic_evaluation_gate() -> None:
    report = run("none", write=False)
    overall = report["summary"]["all"]
    assert overall["isolation_violations"] == 0
    assert overall["authorization_violations"] == 0
    assert overall["safety_adherence"] == 1.0
    assert report["retrieval"]["isolation"]["violations"] == 0
    assert report["gate"]["passed"], report["gate"]["failures"]
