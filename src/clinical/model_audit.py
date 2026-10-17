"""Expose calibration evidence only when it matches the loaded artifact files."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=4)
def _hash(path: str, modified_ns: int, size: int) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def calibration_summary(report_path: Path, model_path: Path, metadata_path: Path) -> dict[str, Any] | None:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        provenance = report["provenance"]
        for path in (model_path, metadata_path):
            info = path.stat()
            if _hash(str(path), info.st_mtime_ns, info.st_size) != provenance[path.name]:
                return None
        calibration = report["test"]["calibration"]
        return {
            "model_version": report["model_version"],
            "dataset": "held-out UCI Diabetes benchmark",
            **{k: calibration[k] for k in ("brier", "ece", "prevalence", "mean_probability", "rows")},
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None
