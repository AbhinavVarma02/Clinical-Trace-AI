"""Repeat the predeclared UI reliability workload without discarding failures.

Offline: ``python -m evaluation.reliability --output evaluation/results/reliability_offline.json``.
Live runs require an explicit free-edge origin, model, bearer-token environment
variable, neuron cap and physical-request cap. The existing agent/provider execute
every run; captured output is measurement data, never replayed inference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

os.environ.setdefault("CLINICAL_TRACE_SKIP_DOTENV", "1")

from src.agent.llm import OpenAICompatibleProvider, ProviderUnavailable, extract_json  # noqa: E402
from src.agent.tools import ToolContext, run_tool  # noqa: E402
from src.clinical.fhir_models import patient_id_of  # noqa: E402
from src.runtime import Runtime, build_runtime  # noqa: E402
from src.safety.policy import PERSONAS, AccessContext  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODEL = "@cf/qwen/qwen3-30b-a3b-fp8"
PROTOCOL = "ui-reliability-v1"


@dataclass(frozen=True)
class Scenario:
    id: str
    patient_id: str
    question: str
    source: str


def load_scenarios(root: Path = ROOT) -> list[Scenario]:
    """Read only the declared development questions, never the held-out cases."""
    demos = (root / "frontend/workspace/demos.ts").read_text(encoding="utf-8")
    block = demos.split("export const DEMOS:", 1)[1].split("export const SUGGESTED_PROMPTS", 1)[0]
    rows = re.findall(r'id:\s*"([^"]+)".*?patientId:\s*"([^"]+)".*?question:\s*"([^"]+)"', block, re.S)
    if len(rows) != 4:
        raise ValueError("Expected the four predeclared UI demo scenarios")
    scenarios = [Scenario(i, p, q, "frontend/workspace/demos.ts") for i, p, q in rows]
    guide = (root / "frontend/workspace/guide.ts").read_text(encoding="utf-8")
    guided = re.search(r'GUIDE_CASE\s*=\s*\{\s*patientId:\s*"([^"]+)",\s*question:\s*"([^"]+)"', guide)
    if not guided or (guided[1], guided[2]) != (scenarios[0].patient_id, scenarios[0].question):
        raise ValueError("Guided case changed; predeclare a revised workload before running")
    for scenario_id, question in (
        ("metformin", "Why was metformin stopped?"),
        ("hba1c", "How has HbA1c changed over time?"),
    ):
        if json.dumps(question) not in demos:
            raise ValueError(f"Historical question missing from suggested prompts: {scenario_id}")
        scenarios.append(Scenario(scenario_id, "synthetic_p001", question, "frontend/workspace/demos.ts"))
    return scenarios


def source_fingerprint(root: Path = ROOT) -> dict[str, str]:
    paths = sorted((root / "src").rglob("*.py")) + [
        root / "evaluation/reliability.py",
        root / "frontend/workspace/guide.ts",
        root / "frontend/workspace/demos.ts",
        root / "deploy/cloudflare/src/config.ts",
    ]
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


class BudgetStop(ProviderUnavailable):
    """A measurement limit or unavailable accounting prevents more requests."""


class BudgetTransport(httpx.BaseTransport):
    """Observe actual HTTP attempts and enforce limits before each inference POST.

    Unknown-charge failures reserve the full request estimate. Successful calls
    use edge-reported accounting, with a conservative estimate when unavailable.
    Public usage since preflight also counts against this experiment's cap.
    """

    def __init__(
        self,
        origin: str,
        model: str,
        max_neurons: float,
        max_calls: int,
        reserve_neurons: float = 2000,
        *,
        transport: httpx.BaseTransport | None = None,
        root: Path = ROOT,
    ) -> None:
        super().__init__()
        parsed = urlparse(origin)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".workers.dev"):
            raise ValueError("Live evaluation requires an HTTPS Workers Free edge origin")
        if parsed.path not in ("", "/") or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use an origin without credentials, path, query or fragment")
        if not math.isfinite(max_neurons) or max_neurons <= 0 or max_calls < 1:
            raise ValueError("Explicit positive neuron and request caps are required")
        if not math.isfinite(reserve_neurons) or reserve_neurons < 0:
            raise ValueError("Reserve must be finite and nonnegative")
        catalog = (root / "deploy/cloudflare/src/config.ts").read_text(encoding="utf-8")
        price = re.search(re.escape(json.dumps(model)) + r":\s*\{\s*input:\s*([\d.]+),\s*output:\s*([\d.]+)", catalog)
        if not price:
            raise ValueError("Model has no checked-in free-edge accounting price")
        self.input_price, self.output_price = float(price[1]), float(price[2])
        self.origin, self.model = origin.rstrip("/"), model
        self.max_neurons, self.max_calls, self.reserve_neurons = max_neurons, max_calls, reserve_neurons
        self.client = httpx.Client(timeout=30, transport=transport)
        self.attempts: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.spent = 0.0
        self.initial_used: float | None = None
        self.initial_day: str | None = None
        self.stop_reason: str | None = None
        self.preflight: dict[str, Any] | None = None

    def _stop(self, reason: str) -> None:
        self.stop_reason = reason
        raise BudgetStop(reason)

    def status(self) -> dict[str, Any]:
        try:
            response = self.client.get(f"{self.origin}/edge/status")
            response.raise_for_status()
            status = response.json()
            inference = status["inference"]
            used, cap = inference["neurons_used_today"], inference["daily_neuron_budget"]
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (used, cap)):
                self._stop("edge accounting is unavailable")
            if status.get("accounting") != "ok" or used < 0 or cap <= 0 or cap > 9500:
                self._stop("edge accounting is unavailable or exceeds its free allocation guard")
            if inference.get("provider") != "cloudflare-workers-ai" or inference.get("model") != self.model:
                self._stop("edge provider/model differs from the declared experiment")
            if not inference.get("configured") or inference.get("quota_state") != "available":
                self._stop("edge inference is unconfigured or quota is unavailable")
            day = status["edge"]["served_at"][:10]
            if self.initial_day is not None and day != self.initial_day:
                self._stop("edge accounting day changed; start a new declared experiment")
            if self.initial_used is not None and used < self.initial_used:
                self._stop("edge accounting decreased unexpectedly")
            self.snapshots.append(status)
            return status
        except BudgetStop:
            raise
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            self._stop("edge status unavailable or malformed")
        raise AssertionError("unreachable")

    def start(self) -> dict[str, Any]:
        status = self.status()
        try:
            response = self.client.get(f"{self.origin}/edge/metrics")
            response.raise_for_status()
            metrics = response.json()
            if not isinstance(metrics, dict):
                self._stop("edge metrics malformed")
        except (httpx.HTTPError, ValueError):
            self._stop("edge metrics unavailable")
        inference = status["inference"]
        self.initial_used = float(inference["neurons_used_today"])
        self.initial_day = status["edge"]["served_at"][:10]
        reserve = max(self.reserve_neurons, inference["daily_neuron_budget"] * 0.25)
        if inference["daily_neuron_budget"] - self.initial_used <= reserve:
            self._stop("ordinary-use reserve leaves no evaluation allocation")
        self.preflight = {"status": status, "metrics": metrics, "reserve_neurons": reserve}
        return self.preflight

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.stop_reason:
            raise BudgetStop(self.stop_reason)
        if self.preflight is None:
            self._stop("preflight has not succeeded")
        if str(request.url) != f"{self.origin}/edge/llm/v1/chat/completions" or request.method != "POST":
            self._stop("request escaped the declared inference endpoint")
        if len(self.attempts) >= self.max_calls:
            self._stop("physical inference request cap reached")
        payload = json.loads(request.content)
        if payload.get("model") != self.model:
            self._stop("request model differs from the declared experiment")
        # Match the edge's conservative request estimate: chars/3 and requested output cap.
        chars = sum(len(m["content"]) for m in payload["messages"])
        estimate = (math.ceil(chars / 3) * self.input_price + payload["max_tokens"] * self.output_price) / 11
        status = self.status()
        used, cap = status["inference"]["neurons_used_today"], status["inference"]["daily_neuron_budget"]
        if max(self.spent, used - (self.initial_used or 0)) + estimate > self.max_neurons:
            self._stop("evaluation neuron cap reached before next request")
        if used + estimate + max(self.reserve_neurons, cap * 0.25) > cap:
            self._stop("ordinary-use neuron reserve reached before next request")
        event: dict[str, Any] = {
            "attempt": len(self.attempts) + 1,
            "stage": "planner" if payload["messages"][-1]["content"].startswith("Tools:") else "composer",
            "max_tokens": payload["max_tokens"],
            "estimated_max_neurons": round(estimate, 4),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.attempts.append(event)
        started = time.perf_counter()
        charge = estimate
        try:
            # Forward the original request, preserving the configured bearer token and edge controls.
            response = self.client.send(request)
            event["http_status"] = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = {"unparseable_response": response.text}
            event["response"] = body
            if response.status_code == 200:
                charge = (body.get("edge") or {}).get("estimated_neurons", estimate)
                if not isinstance(charge, (int, float)) or not math.isfinite(charge) or charge < 0:
                    charge = estimate
                try:
                    raw = body["choices"][0]["message"]["content"]
                    event["raw_output"] = raw
                    parsed = extract_json(raw)
                    event["parsed_output"] = parsed
                    event["parse_failure"] = None
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    event["parse_failure"] = type(exc).__name__
            elif response.status_code == 429:
                self.stop_reason = "edge quota or request limit returned HTTP 429"
            elif response.status_code in (401, 403):
                self.stop_reason = "edge rejected the configured inference credential"
            return response
        except httpx.HTTPError as exc:
            event["transport_error"] = type(exc).__name__
            raise
        finally:
            self.spent += charge
            event["charged_or_reserved_neurons"] = round(charge, 4)
            event["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)

    def close(self) -> None:
        self.client.close()


def citation_checks(rt: Runtime, result: dict[str, Any]) -> dict[str, Any]:
    """Check identity/patient/version resolution; this is not semantic proof."""
    by_id = {e["evidence_id"]: e for e in result.get("evidence", [])}
    rows = []
    uncited = 0
    derived: dict[str, list[Any]] = {}
    derived_tools = {
        "encounter_interval": "get_patient_encounters",
        "completeness_check": "detect_missing_information",
        "consistency_check": "detect_inconsistencies",
        "model_output": "get_readmission_prediction",
        "model_explanation": "get_model_explanations",
    }
    for index, claim in enumerate(result.get("answer", {}).get("claims", [])):
        uncited += not bool(claim.get("citations"))
        for citation in claim.get("citations", []):
            item = by_id.get(citation)
            reason = None
            if item is None:
                reason = "citation absent from this run's evidence"
            elif item["patient_id"] != result["patient_id"]:
                reason = "evidence belongs to another patient"
            else:
                try:
                    resource_type, resource_id = item["source_ref"].split("/", 1)
                    version = str(item["source_version"])
                    resource = rt.store.get(resource_type, resource_id, int(version))
                    if resource is None:
                        reason = "source version is absent from fixture"
                    elif patient_id_of(resource) != result["patient_id"]:
                        reason = "source version belongs to another patient"
                    elif str((resource.get("meta") or {}).get("versionId")) != version:
                        reason = "resolved source has a different version"
                    elif item.get("kind") == "retrieved":
                        chunk = rt.index.chunk(result["patient_id"], citation)
                        if (
                            chunk is None
                            or chunk.source_ref != item["source_ref"]
                            or str(chunk.source_version) != version
                        ):
                            reason = "retrieved chunk source/version differs from the patient index"
                    elif citation.startswith(("check:", "model:", "shap:")):
                        tool = derived_tools.get(item.get("kind", ""))
                        if tool and tool not in derived:
                            context = ToolContext(
                                AccessContext(PERSONAS["clinician.demo"]),
                                rt.policy,
                                rt.records,
                                rt.index,
                                result["patient_id"],
                                rt.agent.predictor,
                            )
                            derived[tool] = run_tool(context, tool).evidence
                        if not tool or not any(
                            e.evidence_id == citation
                            and e.source_ref == item["source_ref"]
                            and str(e.source_version) == version
                            for e in derived[tool]
                        ):
                            reason = "derived evidence has no matching tool source/version lineage"
                    elif citation != f"{resource_id}@v{version}":
                        reason = "citation identifier and source version differ"
                except (KeyError, TypeError, ValueError):
                    reason = "malformed source reference/version"
            rows.append({"claim_index": index, "citation": citation, "valid": reason is None, "reason": reason})
    return {
        "checked": len(rows),
        "invalid": sum(not r["valid"] for r in rows),
        "uncited_claims": uncited,
        "valid": all(r["valid"] for r in rows) and not uncited,
        "citations": rows,
        "scope": "same-run evidence and fixture source/patient/exact-version resolution; no semantic entailment proof",
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    llm_rows = [r for r in records if r.get("llm_counts") is not None]
    drafted = sum(r["llm_counts"]["drafted"] for r in llm_rows)
    excluded = sum(r["llm_counts"]["excluded"] for r in llm_rows)
    latencies = sorted(r["latency_ms"] for r in records)
    return {
        "runs_attempted": len(records),
        "errors": sum("error" in r for r in records),
        "llm_composed": sum(r.get("final_composer", "").startswith("llm:") for r in records),
        "deterministic_fallbacks": sum(r.get("fallback_used", False) for r in records),
        "llm_claims_drafted": drafted,
        "llm_claims_accepted": sum(r["llm_counts"]["accepted"] for r in llm_rows),
        "llm_claims_excluded": excluded,
        "excluded_llm_claim_rate": round(excluded / drafted, 4) if drafted else None,
        "runs_with_llm_exclusions": sum(r["llm_counts"]["excluded"] > 0 for r in llm_rows),
        "citation_errors": sum(r.get("citation_checks", {}).get("invalid", 0) for r in records),
        "parse_failures": sum(bool(e.get("parse_failure")) for r in records for e in r.get("provider_attempts", [])),
        "composer_parse_failures": sum(
            bool(e.get("parse_failure")) and e.get("stage") == "composer"
            for r in records
            for e in r.get("provider_attempts", [])
        ),
        "latency_p50_ms": round(statistics.median(latencies), 2) if latencies else None,
        "latency_p95_ms": round(latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)], 2) if latencies else None,
    }


def _write_report(output: Path | None, report: dict[str, Any]) -> None:
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(output)


def run(
    *,
    repetitions: int = 8,
    scenario_ids: list[str] | None = None,
    output: Path | None = None,
    label: str = "offline",
    source_revision: str | None = None,
    edge_origin: str | None = None,
    model: str = MODEL,
    api_key_env: str = "CLINICAL_TRACE_LLM_API_KEY",
    max_neurons: float | None = None,
    max_calls: int | None = None,
    reserve_neurons: float = 2000,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("Repetitions must be positive")
    if output is not None and output.exists():
        raise ValueError("Refusing to overwrite a prior experiment; use a new output path")
    scenarios = load_scenarios()
    if scenario_ids is not None:
        if not scenario_ids or len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("Select at least one scenario without duplicate identifiers")
        unknown = set(scenario_ids) - {scenario.id for scenario in scenarios}
        if unknown:
            raise ValueError(f"Unknown reliability scenario identifiers: {', '.join(sorted(unknown))}")
        scenarios = [scenario for scenario in scenarios if scenario.id in scenario_ids]
    report: dict[str, Any] = {
        "protocol": PROTOCOL,
        "label": label,
        "source_revision": source_revision,
        "source_sha256": source_fingerprint(),
        "repetitions": repetitions,
        "planned_runs": repetitions * len(scenarios),
        "order": "repetition-major; declared scenario order",
        "questions": [asdict(s) for s in scenarios],
        "mode": "live-llm" if edge_origin else "deterministic-offline",
        "model": model if edge_origin else None,
        "readmission_model_enabled": False,
        "limits": {"max_neurons": max_neurons, "max_calls": max_calls, "reserve_neurons": reserve_neurons},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {"python": platform.python_version(), "platform": platform.system()},
        "historical_reference": {
            "source": "docs/UI_POLISH.md",
            "runs": 48,
            "llm_composed": 44,
            "fallbacks": 4,
            "drafted": 250,
            "accepted": 203,
            "excluded": 47,
            "comparability": "historical aggregate only; not a fresh control",
        },
        "runs": [],
        "completed": False,
        "stop_reason": None,
    }
    budget = None
    provider = None
    rt = None
    _write_report(output, report)
    try:
        if edge_origin:
            token = os.getenv(api_key_env)
            if not token:
                raise BudgetStop("declared credential environment variable is missing")
            if max_neurons is None or max_calls is None:
                raise BudgetStop("live evaluation requires explicit neuron and physical-request caps")
            budget = BudgetTransport(edge_origin, model, max_neurons, max_calls, reserve_neurons, transport=transport)
            report["preflight"] = budget.start()
            provider = OpenAICompatibleProvider(
                f"{edge_origin.rstrip('/')}/edge/llm/v1",
                model,
                "cloudflare-workers-ai",
                api_key=token,
                timeout_s=120,
                transport=budget,
            )
        rt = build_runtime(":memory:", provider=provider, enable_model=False)
        access = AccessContext(PERSONAS["clinician.demo"])
        for repetition in range(1, repetitions + 1):
            for scenario in scenarios:
                if budget and budget.stop_reason:
                    raise BudgetStop(budget.stop_reason)
                start_attempt = len(budget.attempts) if budget else 0
                before = provider.describe() if provider else None
                started = time.perf_counter()
                row: dict[str, Any] = {
                    **asdict(scenario),
                    "repetition": repetition,
                    "sequence": len(report["runs"]) + 1,
                }
                try:
                    result = rt.agent.run(access, scenario.patient_id, scenario.question)
                    inference = result["inference"]
                    attempt = inference.get("llm_attempt")
                    llm_counts = None
                    if attempt is not None:
                        llm_counts = {
                            "drafted": attempt.get("claims_total", 0),
                            "accepted": attempt.get("claims_supported", 0),
                            "excluded": len(attempt.get("rejected", [])),
                        }
                    row.update(
                        outcome=result["outcome"],
                        agent_latency_ms=result.get("latency_ms"),
                        final_composer=inference.get("composer") or "",
                        fallback_used=inference.get("fallback_used", False),
                        fallback_reason=(
                            inference.get("degraded_reason")
                            or (attempt or {}).get("abstain_reason")
                            or ("LLM verification failed" if inference.get("fallback_used") else None)
                        ),
                        llm_counts=llm_counts,
                        final_counts={
                            "drafted": (result.get("verification") or {}).get("claims_total", 0),
                            "accepted": len(result["answer"]["claims"]),
                            "excluded": len(result["answer"]["rejected_claims"]),
                        },
                        citation_checks=citation_checks(rt, result),
                        result=result,
                    )
                except Exception as exc:
                    # Retain an unsuccessful run; exception text can contain endpoint credentials, so omit it.
                    row["error"] = type(exc).__name__
                row["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                row["provider_before"] = before
                row["provider_after"] = provider.describe() if provider else None
                row["provider_attempts"] = budget.attempts[start_attempt:] if budget else []
                report["runs"].append(row)
                report["summary"] = summarize(report["runs"])
                report["by_scenario"] = {
                    s.id: summarize([r for r in report["runs"] if r["id"] == s.id]) for s in scenarios
                }
                report["provider"] = provider.describe() if provider else None
                _write_report(output, report)
                if budget and budget.stop_reason:
                    raise BudgetStop(budget.stop_reason)
        report["completed"] = True
    except (BudgetStop, ValueError) as exc:
        report["stop_reason"] = str(exc)
    finally:
        if budget:
            # Read-only final accounting; unavailable telemetry does not erase any attempts.
            report["postflight"] = {}
            for endpoint in ("status", "metrics"):
                try:
                    response = budget.client.get(f"{budget.origin}/edge/{endpoint}")
                    response.raise_for_status()
                    report["postflight"][endpoint] = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    report["postflight"][endpoint] = {"unavailable": type(exc).__name__}
            report["budget"] = {
                "charged_or_reserved_neurons": round(budget.spent, 4),
                "physical_requests": len(budget.attempts),
                "status_snapshots": budget.snapshots,
                "stop_reason": budget.stop_reason,
            }
        if rt:
            rt.close()
        elif budget:
            budget.close()
        report["summary"] = summarize(report["runs"])
        report["unattempted_runs"] = report["planned_runs"] - len(report["runs"])
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_report(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--scenario", dest="scenario_ids", action="append", choices=[s.id for s in load_scenarios()])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="offline")
    parser.add_argument("--source-revision")
    parser.add_argument("--provider", choices=["none", "cloudflare-workers-ai"], default="none")
    parser.add_argument("--edge-origin")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--api-key-env", default="CLINICAL_TRACE_LLM_API_KEY")
    parser.add_argument("--max-neurons", type=float)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--reserve-neurons", type=float, default=2000)
    args = parser.parse_args()
    if args.provider != "none" and not args.edge_origin:
        parser.error("live provider requires --edge-origin")
    if args.provider == "none" and args.edge_origin:
        parser.error("--edge-origin requires the live provider flag")
    report = run(**{k: v for k, v in vars(args).items() if k != "provider"})
    print(
        json.dumps(
            {
                "completed": report["completed"],
                "stop_reason": report["stop_reason"],
                "summary": report["summary"],
                "output": str(args.output),
            },
            indent=2,
        )
    )
    if not report["completed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
