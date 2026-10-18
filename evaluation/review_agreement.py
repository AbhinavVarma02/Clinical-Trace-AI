"""Compute human-review agreement only from supplied, complete paired annotations.

No annotations are generated and no agreement result exists until two actual
reviewers supply ratings. Artificial labels in unit tests validate formulas only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import warnings
from pathlib import Path
from typing import Any

from sklearn.metrics import cohen_kappa_score

FIELDS = ("grounding", "correctness", "safety", "review_appropriateness")
LABELS = ("pass", "fail", "uncertain")


def agreement(rows: list[dict[str, str]], known_cases: set[str]) -> dict[str, Any]:
    if not rows:
        raise ValueError("actual paired reviewer annotations are required")
    reviewers = sorted({row["reviewer_id"] for row in rows})
    if len(reviewers) != 2 or not all(reviewers):
        raise ValueError("exactly two nonempty reviewer identifiers are required")
    paired: dict[str, dict[str, dict[str, str]]] = {}
    for row in rows:
        case, reviewer = row["case_id"], row["reviewer_id"]
        if case not in known_cases:
            raise ValueError("annotation references an unknown case")
        if reviewer in paired.setdefault(case, {}):
            raise ValueError("duplicate case/reviewer annotation")
        if any(row.get(field) not in LABELS for field in FIELDS):
            raise ValueError("all rubric ratings must be pass, fail or uncertain")
        paired[case][reviewer] = row
    if any(len(pair) != 2 for pair in paired.values()):
        raise ValueError("every included case requires both reviewers; no partial cases are silently dropped")
    metrics = {}
    for field in FIELDS:
        a = [paired[case][reviewers[0]][field] for case in sorted(paired)]
        b = [paired[case][reviewers[1]][field] for case in sorted(paired)]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            kappa = float(cohen_kappa_score(a, b, labels=list(LABELS)))
        metrics[field] = {
            "agreement": sum(x == y for x, y in zip(a, b)) / len(a),
            "cohen_kappa": kappa if math.isfinite(kappa) else None,
            "kappa_note": None if math.isfinite(kappa) else "undefined when expected agreement is one",
            "uncertain_ratings": a.count("uncertain") + b.count("uncertain"),
        }
    return {
        "rubric_version": "v1",
        "cases": len(paired),
        "reviewers": 2,
        "metrics": metrics,
        "limitations": [
            "Agreement does not establish clinical validity or factual correctness.",
            "Reviewers must annotate independently before reconciliation.",
            "No case-level ratings, reviewer identities or free-text notes are exported.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    datasets = Path(__file__).parent / "datasets"
    known = {
        json.loads(line)["id"]
        for path in datasets.glob("agent_*_v1.jsonl")
        for line in path.read_text().splitlines()
        if line.strip()
    }
    with args.annotations.open(newline="", encoding="utf-8") as handle:
        report = agreement(list(csv.DictReader(handle)), known)
    report["annotation_sha256"] = hashlib.sha256(args.annotations.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
