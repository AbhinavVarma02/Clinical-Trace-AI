"""The workspace's static evidence snapshot must match the committed evaluation artifacts exactly."""

from __future__ import annotations

from evaluation.evidence_snapshot import SNAPSHOT, build, render


def test_snapshot_matches_result_artifacts() -> None:
    assert SNAPSHOT.read_text(encoding="utf-8").replace("\r\n", "\n") == render(build()), (
        "run: python -m evaluation.evidence_snapshot"
    )


def test_snapshot_discloses_calibration_and_scoring_limits() -> None:
    snapshot = build()
    assert snapshot["readmission"]["mean_probability"] > snapshot["readmission"]["prevalence"]
    assert "poorly calibrated" in snapshot["readmission"]["warning"]
    assert "no clinical validation" in snapshot["note"]
    assert all(len(source["sha256"]) == 64 for source in snapshot["sources"])
