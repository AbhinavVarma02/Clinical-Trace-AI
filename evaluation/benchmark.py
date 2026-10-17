"""Measure the real local HTTP/agent path on synthetic workloads, with no LLM.

TestClient includes routing/middleware/database/graph work but no TCP, TLS,
browser rendering or container startup. Results are not public-deployment SLAs.
"""

from __future__ import annotations

import os

os.environ["CLINICAL_TRACE_SKIP_DOTENV"] = "1"

import argparse
import json
import platform
import subprocess
import tempfile
import time
import tracemalloc
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from api.main import app
from api.middleware import RATE_LIMITER
from src.persistence.db import utc_now
from src.runtime import build_runtime, set_runtime


def benchmark(requests: int = 40, concurrency: tuple[int, ...] = (1, 4, 8)) -> dict:
    if requests < 1 or not concurrency or min(concurrency) < 1:
        raise ValueError("positive requests/concurrency are required")
    scenarios = [
        ("synthetic_p001", "Why was metformin stopped?"),
        ("synthetic_p002", "Is the smoking history consistent across notes?"),
        ("synthetic_p003", "What information is missing from this record?"),
        ("synthetic_p004", "How has the warfarin dose changed and what were the INR values?"),
    ]
    rows = []
    old_limits = RATE_LIMITER.limits.copy()
    RATE_LIMITER.limits.update(default=100000, agent=100000)
    try:
        with tempfile.TemporaryDirectory(prefix="ct-benchmark-") as scratch:
            started = time.perf_counter()
            rt = build_runtime(Path(scratch) / "benchmark.db", provider=None, enable_model=False)
            initialization_ms = (time.perf_counter() - started) * 1000
            set_runtime(rt)
            try:
                with TestClient(app) as client:
                    # Warm routing/imports separately from the timed workload.
                    client.get("/api/v2/system/status")

                    def request(index):
                        patient, question = scenarios[index % len(scenarios)]
                        start = time.perf_counter()
                        response = client.post(
                            "/api/v2/agent/runs",
                            headers={"X-Demo-Persona": "clinician.demo"},
                            json={"patient_id": patient, "question": question},
                        )
                        latency = (time.perf_counter() - start) * 1000
                        result = response.json()
                        isolated = response.status_code != 200 or (
                            result["patient_id"] == patient
                            and all(e["patient_id"] == patient for e in result.get("evidence", []))
                        )
                        return latency, response.status_code, result.get("outcome"), isolated

                    for count in concurrency:
                        start = time.perf_counter()
                        with ThreadPoolExecutor(max_workers=count) as pool:
                            outcomes = list(pool.map(request, range(requests)))
                        elapsed = time.perf_counter() - start
                        latencies = [r[0] for r in outcomes]
                        successful = [r[0] for r in outcomes if r[1] == 200]
                        rows.append(
                            {
                                "concurrency": count,
                                "requests": requests,
                                "elapsed_s": elapsed,
                                "offered_throughput_rps": requests / elapsed,
                                "successful_throughput_rps": len(successful) / elapsed,
                                "status_codes": dict(Counter(str(r[1]) for r in outcomes)),
                                "outcomes": dict(Counter(str(r[2]) for r in outcomes)),
                                "p50_ms": float(np.percentile(latencies, 50)),
                                "p95_ms": float(np.percentile(latencies, 95)),
                                "successful_p95_ms": float(np.percentile(successful, 95)) if successful else None,
                                "http_error_rate": sum(r[1] >= 400 for r in outcomes) / requests,
                                "isolation_violations": sum(not r[3] for r in outcomes),
                            }
                        )
                    # Allocation profiling is separate so it does not distort latency results.
                    tracemalloc.start()
                    request(0)
                    _, allocation_peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    diagnostics = rt.agent.describe()
            finally:
                set_runtime(None)
                rt.close()
    finally:
        RATE_LIMITER.limits.update(old_limits)
        RATE_LIMITER._buckets.clear()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "source_commit": revision,
        "environment": {
            "os": platform.system(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "mode": "deterministic",
            "risk_model": "disabled",
        },
        "runtime_initialization_ms": initialization_ms,
        "single_request_python_allocation_peak_bytes": allocation_peak,
        "execution": diagnostics,
        "workloads": rows,
        "limitations": [
            "In-process HTTP; no network/container/cold-process or live-inference claim.",
            "One run on local hardware; cache/warmup and scheduler affect timings.",
            "Python allocation peak excludes native libraries and total process RSS.",
            "HTTP errors include deliberate capacity rejection; successful latency is reported separately.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--output", type=Path, default=Path("evaluation/results/performance_local.json"))
    args = parser.parse_args()
    report = benchmark(args.requests)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report["workloads"], indent=2))


if __name__ == "__main__":
    main()
