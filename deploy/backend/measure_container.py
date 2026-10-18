"""Measure the backend container under a given resource shape (cold start, memory, workflow latency).

    python deploy/backend/measure_container.py IMAGE --memory 512m --cpus 0.1 --out result.json

Starts a fresh container, times readiness, runs the guided workflow (agent run, review, approval)
and the readmission risk/SHAP call, samples `docker stats` memory throughout, and records whether
the container was OOM-killed. Requires a local Docker daemon. Uses only synthetic demo data.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


def _call(
    base: str, path: str, persona: str, workspace: str, body: dict[str, Any] | None = None, timeout: float = 300
) -> tuple[int, Any, float]:
    headers = {"X-Demo-Persona": persona, "X-Demo-Workspace": workspace, "Content-Type": "application/json"}
    request = urllib.request.Request(
        base + path,
        headers=headers,
        method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read()), (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        return exc.code, None, (time.perf_counter() - started) * 1000


def _mib(value: str) -> float:
    number, unit = float(value[:-3] if value[-3:] in ("GiB", "MiB", "KiB") else value[:-1]), value[-3:]
    return number * {"GiB": 1024, "MiB": 1, "KiB": 1 / 1024}.get(unit, 1 / 1024 / 1024)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--memory", default="512m")
    parser.add_argument("--cpus", default="0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--out")
    args = parser.parse_args()

    name = f"ct-measure-{secrets.token_hex(3)}"
    base = f"http://127.0.0.1:{args.port}"
    started = time.perf_counter()
    _docker(
        "run",
        "-d",
        "--name",
        name,
        f"--memory={args.memory}",
        f"--memory-swap={args.memory}",
        f"--cpus={args.cpus}",
        "-p",
        f"{args.port}:8000",
        args.image,
    )
    samples: list[float] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            try:
                samples.append(_mib(_docker("stats", "--no-stream", "--format", "{{.MemUsage}}", name).split(" / ")[0]))
            except (subprocess.CalledProcessError, ValueError):
                pass
            stop.wait(1.0)

    threading.Thread(target=sample, daemon=True).start()
    result: dict[str, Any] = {"image": args.image, "memory_limit": args.memory, "cpus": args.cpus}
    try:
        ready_ms = None
        while time.perf_counter() - started < 600:
            try:
                with urllib.request.urlopen(base + "/api/v2/system/status", timeout=5) as response:
                    status = json.loads(response.read())
                    ready_ms = (time.perf_counter() - started) * 1000
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(1)
        result["cold_start_to_ready_ms"] = round(ready_ms) if ready_ms else None
        if ready_ms is None:
            raise RuntimeError("container never became ready")
        result["model_loaded"] = status["readmission_model"]["loaded"]
        ws = secrets.token_hex(16)
        steps: dict[str, Any] = {}
        code, run, ms = _call(
            base,
            "/api/v2/agent/runs",
            "clinician.demo",
            ws,
            {"patient_id": "synthetic_p001", "question": "Summarize this patient journey"},
        )
        steps["first_agent_run"] = {"status": code, "ms": round(ms), "outcome": (run or {}).get("outcome")}
        code, _, ms = _call(base, "/api/v2/patients/synthetic_p001/risk", "clinician.demo", ws)
        steps["risk_with_shap"] = {"status": code, "ms": round(ms)}
        code, review, ms = _call(base, "/api/v2/reviews", "clinician.demo", ws, {"run_id": run["run_id"]})
        steps["submit_review"] = {"status": code, "ms": round(ms)}
        code, decided, ms = _call(
            base,
            f"/api/v2/reviews/{review['review_id']}/decision",
            "reviewer.demo",
            ws,
            {"decision": "approved", "artifact_hash": review["artifact_hash"]},
        )
        steps["approve"] = {"status": code, "ms": round(ms), "valid": (decided or {}).get("approval_valid")}
        warm = []
        for question in (
            "Why was metformin stopped?",
            "How has HbA1c changed over time?",
            "What is the readmission risk and what drives it?",
            "Was there a readmission within 30 days?",
        ):
            code, body, ms = _call(
                base, "/api/v2/agent/runs", "clinician.demo", ws, {"patient_id": "synthetic_p001", "question": question}
            )
            warm.append({"status": code, "ms": round(ms), "outcome": (body or {}).get("outcome")})
        steps["warm_agent_runs"] = warm
        result["steps"] = steps
        result["workflow_ok"] = all(s["status"] == 200 for s in warm) and steps["approve"]["valid"] is True
    except Exception as exc:  # noqa: BLE001 - record the failure as evidence
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        time.sleep(2)
        stop.set()
        state = json.loads(_docker("inspect", name, "--format", "{{json .State}}"))
        result["oom_killed"] = state.get("OOMKilled")
        result["container_running"] = state.get("Running")
        result["memory_peak_sampled_mib"] = round(max(samples), 1) if samples else None
        result["memory_samples"] = len(samples)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    text = json.dumps(result, indent=1)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0 if result.get("workflow_ok") and not result.get("oom_killed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
