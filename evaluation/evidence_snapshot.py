"""Build the static evidence snapshot shown by the workspace before the backend is reachable.

The public edge serves the React workspace instantly, while the Python backend may still be
starting. The overview therefore shows measured results from committed evaluation artifacts.
Every number is copied from a result file whose SHA-256 is recorded next to it; nothing is
computed, rounded beyond display precision, or invented here.

    python -m evaluation.evidence_snapshot          # rewrite the snapshot
    python -m evaluation.evidence_snapshot --check  # fail if it is stale (used by tests/CI)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evaluation" / "results"
SNAPSHOT = ROOT / "frontend" / "workspace" / "evidence-snapshot.json"

AGENT_RUNS = [
    ("latest.json", "Deterministic (no language model)", "local CPU"),
    (
        "llm_cf_qwen_qwen3-30b-a3b-fp8.json",
        "Cloudflare Workers AI · Qwen3-30B-A3B",
        "local backend → Workers AI (free allocation)",
    ),
    ("llm_qwen2.5_7b_durable.json", "Local Ollama · Qwen2.5 7B", "development desktop"),
]


def _sha256(path: Path) -> str:
    # Hash with normalised line endings so Windows and Linux checkouts agree.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _round(value: Any, digits: int = 4) -> Any:
    return round(value, digits) if isinstance(value, float) else value


def build() -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    agent: list[dict[str, Any]] = []
    for name, label, where in AGENT_RUNS:
        path = RESULTS / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        overall = data["summary"]["all"]
        sources.append(
            {
                "file": f"evaluation/results/{name}",
                "sha256": _sha256(path),
                "run_at": data.get("run_at"),
                "git_commit": data.get("git_commit"),
            }
        )
        provider = data.get("provider") or {}
        agent.append(
            {
                "label": label,
                "where": where,
                "mode": data.get("mode"),
                "model": provider.get("model"),
                "dataset_version": data.get("dataset_version"),
                "cases": overall["cases"],
                "passed": overall["passed"],
                "gate_passed": data["gate"]["passed"],
                "answer_correctness": _round(overall.get("answer_correctness")),
                "unsupported_claim_rate": _round(overall.get("unsupported_claim_rate")),
                "safety_adherence": _round(overall.get("safety_adherence")),
                "isolation_violations": overall.get("isolation_violations"),
                "latency_p50_ms": _round(overall.get("latency_p50_ms"), 1),
                "latency_p95_ms": _round(overall.get("latency_p95_ms"), 1),
                "run_at": data.get("run_at"),
                "git_commit": data.get("git_commit"),
            }
        )
    latest = json.loads((RESULTS / "latest.json").read_text(encoding="utf-8"))
    hybrid = latest["retrieval"]["modes"]["hybrid"]["all"]
    report_path = RESULTS / "readmission_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    sources.append(
        {
            "file": "evaluation/results/readmission_report.json",
            "sha256": _sha256(report_path),
            "run_at": report.get("generated_at"),
            "git_commit": report.get("source_commit"),
        }
    )
    metrics, calibration = report["test"]["metrics"], report["test"]["calibration"]
    return {
        "schema_version": 1,
        "note": "Copied from committed evaluation artifacts (see sources). Scoring is rule-based against versioned "
        "synthetic cases; there is no LLM judge, no human rating and no clinical validation.",
        "agent_evaluations": agent,
        "retrieval": {
            "mode": "hybrid",
            "queries": hybrid["queries"],
            "recall_at_5": hybrid["recall_at_5"],
            "mrr_at_10": hybrid["mrr_at_10"],
            "ndcg_at_10": hybrid["ndcg_at_10"],
        },
        "readmission": {
            "dataset": "UCI Diabetes 130-US hospitals (public benchmark), patient-level held-out test split",
            "model_version": report.get("model_version"),
            "test_rows": calibration.get("rows"),
            "roc_auc": _round(metrics["roc_auc"]),
            "pr_auc": _round(metrics["pr_auc"]),
            "threshold": metrics["threshold"],
            "recall_positive": _round(metrics["recall_positive"]),
            "precision_positive": _round(metrics["precision_positive"]),
            "brier": _round(calibration["brier"]),
            "ece": _round(calibration["ece"]),
            "prevalence": _round(calibration["prevalence"]),
            "mean_probability": _round(calibration["mean_probability"]),
            "warning": "Scores are poorly calibrated: the mean predicted probability is far above the observed "
            "readmission rate, so they must not be read as patient-level probabilities.",
        },
        "sources": sources,
    }


def render(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed snapshot is stale")
    args = parser.parse_args()
    expected = render(build())
    if args.check:
        current = SNAPSHOT.read_text(encoding="utf-8").replace("\r\n", "\n") if SNAPSHOT.exists() else ""
        if current != expected:
            print(f"{SNAPSHOT.relative_to(ROOT)} is stale; run python -m evaluation.evidence_snapshot", file=sys.stderr)
            return 1
        print("evidence snapshot is current")
        return 0
    SNAPSHOT.write_text(expected, encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
