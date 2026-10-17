"""Reference-fitted data/prediction PSI diagnostics; never auto-retrain.

Thresholds are operational heuristics, not significance tests. Missingness has
its own bin. Bins/vocabularies use reference data only. No temporal claims.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.persistence.db import utc_now
from src.preprocessing import MODEL_FEATURE_COLUMNS

SMOOTHING = 1e-6
ALLOWED_COLUMNS = frozenset(MODEL_FEATURE_COLUMNS) | {"age"}


def _compare(reference: np.ndarray, current: np.ndarray) -> dict[str, Any]:
    p, q = reference.astype(float) + SMOOTHING, current.astype(float) + SMOOTHING
    p, q = p / p.sum(), q / q.sum()
    psi = float(np.sum((q - p) * np.log(q / p)))
    return {"psi": psi, "alert": psi >= 0.2, "reference_counts": reference.tolist(),
            "current_counts": current.tolist()}


def numeric_drift(reference: pd.Series, current: pd.Series, bins: int = 10) -> dict[str, Any]:
    if not len(reference) or not len(current) or bins < 1:
        raise ValueError("nonempty windows and a positive bin count are required")
    ref = pd.to_numeric(reference, errors="coerce").replace([np.inf, -np.inf], np.nan)
    cur = pd.to_numeric(current, errors="coerce").replace([np.inf, -np.inf], np.nan)
    finite = ref.dropna().to_numpy()
    if not len(finite):
        return {"available": False, "reason": "reference has no finite values",
                "reference_missing_rate": 1.0, "current_missing_rate": float(cur.isna().mean())}
    cuts = np.unique(np.quantile(finite, np.linspace(0, 1, bins + 1)[1:-1]))
    if np.ptp(finite) == 0:
        cuts = np.array([np.nextafter(finite[0], -np.inf), np.nextafter(finite[0], np.inf)])
    edges = np.r_[-np.inf, cuts, np.inf]
    r = np.r_[np.histogram(ref.dropna(), bins=edges)[0], ref.isna().sum()]
    c = np.r_[np.histogram(cur.dropna(), bins=edges)[0], cur.isna().sum()]
    return {"available": True, **_compare(r, c), "reference_cutpoints": cuts.tolist(),
            "last_bin": "missing_or_non_numeric", "reference_missing_rate": float(ref.isna().mean()),
            "current_missing_rate": float(cur.isna().mean())}


def categorical_drift(reference: pd.Series, current: pd.Series, top_n: int = 20) -> dict[str, Any]:
    if not len(reference) or not len(current) or top_n < 1:
        raise ValueError("nonempty windows and a positive vocabulary limit are required")
    ref, cur = (v.astype("string").replace("?", pd.NA) for v in (reference, current))
    vocabulary = ref.value_counts().head(top_n).index.tolist()

    def counts(values: pd.Series) -> np.ndarray:
        return np.array([int(values.eq(v).sum()) for v in vocabulary]
                        + [int((values.notna() & ~values.isin(vocabulary)).sum()), int(values.isna().sum())])

    return {"available": True, **_compare(counts(ref), counts(cur)), "reference_vocabulary": vocabulary,
            "last_bins": ["other_or_unseen", "missing"],
            "unseen_rate": float((cur.notna() & ~cur.isin(ref.dropna().unique())).mean()),
            "reference_missing_rate": float(ref.isna().mean()), "current_missing_rate": float(cur.isna().mean())}


def build_report(reference: pd.DataFrame, current: pd.DataFrame, columns: list[str],
                 reference_scores: Any = None, current_scores: Any = None) -> dict[str, Any]:
    if not columns or len(set(columns)) != len(columns) or not set(columns) <= ALLOWED_COLUMNS:
        raise ValueError("choose distinct model feature columns; identifiers/free text are excluded")
    if not len(reference) or not len(current):
        raise ValueError("both windows must contain rows")
    features = {}
    for column in columns:
        if column not in reference or column not in current:
            features[column] = {"available": False, "reason": "column absent from a window"}
            continue
        fn = numeric_drift if pd.api.types.is_numeric_dtype(reference[column]) else categorical_drift
        features[column] = fn(reference[column], current[column])
    predictions = None
    if reference_scores is not None or current_scores is not None:
        for frame, scores in ((reference, reference_scores), (current, current_scores)):
            scores = np.asarray(scores, dtype=float)
            if scores.shape != (len(frame),) or not np.isfinite(scores).all() or np.any((scores < 0) | (scores > 1)):
                raise ValueError("prediction windows must be aligned finite probabilities in [0,1]")
        predictions = numeric_drift(pd.Series(reference_scores), pd.Series(current_scores))
    return {"schema_version": 1, "generated_at": utc_now(),
            "rows": {"reference": len(reference), "current": len(current)},
            "method": {"numeric": "reference quantiles, unbounded outer bins, separate missing bin",
                       "categorical": "reference top-20, other/unseen and missing bins",
                       "smoothing_count": SMOOTHING, "psi_alert_threshold": 0.2},
            "features": features, "predictions": predictions,
            "limitations": ["PSI threshold is a heuristic, not a statistical or clinical conclusion.",
                            "No outcomes used; this cannot establish performance/concept drift.",
                            "Reference/current provenance and temporal meaning must be supplied by the operator.",
                            "No automated retraining, recalibration or deployment is triggered."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--columns", nargs="+", required=True)
    parser.add_argument("--prediction-column")
    parser.add_argument("--output", type=Path, default=Path("monitoring/reports/drift.json"))
    args = parser.parse_args()
    reference, current = (pd.read_csv(p, na_values=["?"]) for p in (args.reference, args.current))
    scores = ((reference[args.prediction_column], current[args.prediction_column]) if args.prediction_column else (None, None))
    report = build_report(reference, current, args.columns, *scores)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Wrote aggregate diagnostics to {args.output}")


if __name__ == "__main__":
    main()
