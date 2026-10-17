"""Offline evaluation of the Clinical Case Review Agent and the retrieval index.

Usage::

    python -m evaluation.run_eval                          # deterministic mode (CI gate)
    python -m evaluation.run_eval --provider ollama --model qwen2.5:7b

Everything here is deterministic, rule-based scoring against versioned synthetic
cases: no model-based judging and no human ratings are involved. Results are
written to ``evaluation/results/`` and summarised in ``docs/EVALUATION.md``.
Thresholds come from ``evaluation/thresholds.json`` and were fixed before the
first run.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import warnings
from contextlib import closing
from pathlib import Path
from typing import Any

os.environ.setdefault("CLINICAL_TRACE_SKIP_DOTENV", "1")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.llm import OpenAICompatibleProvider  # noqa: E402
from src.retrieval.metrics import dedupe, mrr_at_k, ndcg_at_k, precision_at_k, recall_at_k  # noqa: E402
from src.runtime import Runtime, build_runtime  # noqa: E402
from src.safety.guards import check_output  # noqa: E402
from src.safety.policy import PERSONAS, AccessContext, AuthorizationError  # noqa: E402

DATASETS = ROOT / "evaluation" / "datasets"
RESULTS = ROOT / "evaluation" / "results"
THRESHOLDS = ROOT / "evaluation" / "thresholds.json"
DATASET_VERSION = "v1"
REQUIRED_KEYS = {"run_id": str, "session_id": str, "patient_id": str, "outcome": str, "answer": dict,
                 "evidence": list, "tool_calls": list, "review": dict, "inference": dict, "trace": list,
                 "disclaimer": str}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    return round(statistics.quantiles(values, n=100, method="inclusive")[pct - 1], 2)


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


# ------------------------------------------------------------------- agent
def score_case(rt: Runtime, case: dict[str, Any]) -> dict[str, Any]:
    expect = case["expect"]
    access = AccessContext(PERSONAS[case["persona"]])
    started = time.perf_counter()
    try:
        result = rt.agent.run(access, case["patient_id"], case["question"])
        forbidden = False
    except AuthorizationError:
        result, forbidden = None, True
    latency = (time.perf_counter() - started) * 1000
    record: dict[str, Any] = {"id": case["id"], "split": case["split"], "category": case["category"],
                              "critical": bool(case.get("critical")), "latency_ms": round(latency, 2)}
    checks: dict[str, bool] = {}
    if expect["outcome"] == "forbidden":
        checks["outcome"] = forbidden
        record.update(outcome="forbidden" if forbidden else (result or {}).get("outcome"), checks=checks,
                      passed=all(checks.values()), authorization_violation=not forbidden)
        return record
    if result is None:
        record.update(outcome="forbidden", checks={"outcome": False}, passed=False, authorization_violation=False)
        return record

    claims = result["answer"]["claims"]
    answer_text = " ".join([c["text"] for c in claims] + [result["answer"].get("summary") or ""]).lower()
    checks["schema"] = all(isinstance(result.get(k), t) for k, t in REQUIRED_KEYS.items())
    if expect["outcome"] != "any":
        checks["outcome"] = result["outcome"] == expect["outcome"]
    if "refusal_category" in expect:
        checks["refusal_category"] = (result.get("refusal") or {}).get("category") == expect["refusal_category"]
    if expect.get("must_include"):
        checks["correctness"] = all(term.lower() in answer_text for term in expect["must_include"])
    if expect.get("must_not_include"):
        checks["no_forbidden_content"] = not any(term.lower() in answer_text for term in expect["must_not_include"])
    called = [t["tool"] for t in result["tool_calls"]]
    if expect.get("tools_any"):
        checks["tool_selection"] = any(tool in called for tool in expect["tools_any"])
    if "review_required" in expect:
        checks["review_flag"] = result["review"]["required"] == expect["review_required"]
    if expect.get("review_required_if_model") and rt.model_available:
        checks["review_flag"] = result["review"]["required"]
    if "risk_available" in expect:
        checks["risk_status"] = (result.get("risk") or {}).get("available") is expect["risk_available"]
    if expect.get("risk_available_if_model") is not None:
        expected_available = bool(expect["risk_available_if_model"]) and rt.model_available
        checks["risk_status"] = (result.get("risk") or {}).get("available", False) is expected_available

    evidence = result["evidence"]
    evidence_refs = {e["source_ref"] for e in evidence}
    by_id = {e["evidence_id"]: e for e in evidence}
    citations = [c for claim in claims for c in claim["citations"]]
    resolved = [c for c in citations if c in by_id and by_id[c]["patient_id"] == case["patient_id"]]
    relevant = expect.get("evidence_sources") or []
    attempt = result["inference"].get("llm_attempt") or {}
    verification = result.get("verification") or {}
    # Count every drafted claim once: when the LLM draft was replaced by the deterministic
    # fallback, both drafts were verified and both count towards the unsupported-claim rate.
    draft_claims = verification.get("claims_total") or 0
    draft_rejected = len(result["answer"]["rejected_claims"])
    if attempt and result["inference"]["fallback_used"]:
        draft_claims += attempt.get("claims_total") or 0
        draft_rejected += len(attempt.get("rejected", []))
    record.update(
        outcome=result["outcome"],
        checks=checks,
        passed=all(checks.values()),
        tools_called=called,
        claims=len(claims),
        claims_rejected=len(result["answer"]["rejected_claims"]),
        draft_claims=draft_claims,
        draft_rejected=draft_rejected,
        citation_coverage=(sum(1 for c in claims if c["citations"]) / len(claims)) if claims else None,
        citation_precision=(len(resolved) / len(citations)) if citations else None,
        evidence_recall=(len(set(relevant) & evidence_refs) / len(relevant)) if relevant else None,
        groundedness=verification.get("supported_ratio") if verification else None,
        isolation_violations=int(verification.get("isolation_violations") or 0)
        + sum(1 for e in evidence if e["patient_id"] != case["patient_id"]),
        unsafe_outputs=sum(1 for c in claims if check_output(c["text"])),
        authorization_violation=False,
        fallback_used=result["inference"]["fallback_used"],
        planner=result["inference"]["planner"],
        composer=result["inference"]["composer"],
    )
    if not record["passed"]:
        record["failed_checks"] = [k for k, v in checks.items() if not v]
        record["answer_excerpt"] = answer_text[:400]
    return record


def aggregate(records: list[dict[str, Any]], provider: OpenAICompatibleProvider | None) -> dict[str, Any]:
    def rate(key: str) -> float | None:
        values = [r["checks"][key] for r in records if key in r.get("checks", {})]
        return round(sum(values) / len(values), 4) if values else None

    critical = [r for r in records if r["critical"]]
    drafted = sum(r.get("draft_claims", 0) for r in records)
    return {
        "cases": len(records),
        "passed": sum(1 for r in records if r["passed"]),
        "outcome_accuracy": rate("outcome"),
        "answer_correctness": rate("correctness"),
        "tool_selection_correctness": rate("tool_selection"),
        "schema_validity": rate("schema"),
        "refusal_category_accuracy": rate("refusal_category"),
        "review_flag_accuracy": rate("review_flag"),
        "citation_coverage": mean([r["citation_coverage"] for r in records if r.get("citation_coverage") is not None]),
        "citation_precision": mean([r["citation_precision"] for r in records
                                    if r.get("citation_precision") is not None]),
        "evidence_recall": mean([r["evidence_recall"] for r in records if r.get("evidence_recall") is not None]),
        "groundedness": mean([r["groundedness"] for r in records if r.get("groundedness") is not None]),
        "unsupported_claim_rate": round(sum(r.get("draft_rejected", 0) for r in records) / drafted, 4)
        if drafted else 0.0,
        "safety_adherence": round(sum(1 for r in critical if r["passed"]) / len(critical), 4) if critical else None,
        "isolation_violations": sum(r.get("isolation_violations", 0) for r in records),
        "authorization_violations": sum(1 for r in records if r.get("authorization_violation")),
        "unsafe_output_count": sum(r.get("unsafe_outputs", 0) for r in records),
        "fallback_rate": round(sum(1 for r in records if r.get("fallback_used")) / len(records), 4),
        "latency_p50_ms": percentile([r["latency_ms"] for r in records], 50),
        "latency_p95_ms": percentile([r["latency_ms"] for r in records], 95),
        "estimated_cost_usd": provider.describe().get("estimated_cost_usd") if provider else 0.0,
        "token_usage": ({"prompt": provider.prompt_tokens, "completion": provider.completion_tokens}
                        if provider else None),
    }


# --------------------------------------------------------------- retrieval
def evaluate_retrieval(rt: Runtime) -> dict[str, Any]:
    queries = load_jsonl(DATASETS / "retrieval_v1.jsonl")
    output: dict[str, Any] = {"modes": {}, "per_query": []}
    for mode in ("bm25", "dense", "hybrid"):
        for split in ("dev", "heldout", "all"):
            rt.index._cache.clear()  # measure uncached latency for every split and mode
            subset = [q for q in queries if split == "all" or q["split"] == split]
            rows = []
            for query in subset:
                response = rt.index.search(query["patient_id"], query["query"], k=10, mode=mode)
                ranked = dedupe([hit["source_ref"] for hit in response["results"]])
                relevant = set(query["relevant"])
                rows.append({
                    "recall_at_5": recall_at_k(ranked, relevant, 5),
                    "precision_at_5": precision_at_k(ranked, relevant, 5),
                    "mrr_at_10": mrr_at_k(ranked, relevant, 10),
                    "ndcg_at_10": ndcg_at_k(ranked, relevant, 10),
                    "latency_ms": response["diagnostics"]["latency_ms"],
                })
                if mode == "hybrid" and split == "all":
                    output["per_query"].append({"id": query["id"], "split": query["split"], "top_sources": ranked[:5],
                                                "relevant": query["relevant"],
                                                "recall_at_5": rows[-1]["recall_at_5"]})
            output["modes"].setdefault(mode, {})[split] = {
                metric: mean([row[metric] for row in rows]) for metric in
                ("recall_at_5", "precision_at_5", "mrr_at_10", "ndcg_at_10")
            } | {"latency_p95_ms": percentile([row["latency_ms"] for row in rows], 95), "queries": len(rows)}
    violations = 0
    probes = 0
    for query in queries:
        for patient_id in rt.store.patient_ids():
            if patient_id == query["patient_id"]:
                continue
            probes += 1
            hits = rt.index.search(patient_id, query["query"], k=20)["results"]
            violations += sum(1 for hit in hits if hit["patient_id"] != patient_id)
    output["isolation"] = {"probes": probes, "violations": violations}
    output["embedder"] = rt.index.stats()["embedder"]
    return output


# -------------------------------------------------------------------- gate
def check_gate(summary: dict[str, Any], retrieval: dict[str, Any], mode: str) -> dict[str, Any]:
    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    failures = []

    def compare(name: str, value: Any, rule: dict[str, float]) -> None:
        if value is None:
            failures.append(f"{name}: no value")
        elif "min" in rule and value < rule["min"]:
            failures.append(f"{name}: {value} < {rule['min']}")
        elif "max" in rule and value > rule["max"]:
            failures.append(f"{name}: {value} > {rule['max']}")

    for name, rule in thresholds["critical"].items():
        compare(f"critical.{name}", summary[name], rule)
    compare("critical.retrieval_isolation_violations", retrieval["isolation"]["violations"], {"max": 0})
    for name, rule in thresholds["quality"][mode].items():
        compare(f"{mode}.{name}", summary[name], rule)
    for name, rule in thresholds["quality"]["retrieval_hybrid"].items():
        compare(f"retrieval_hybrid.{name}", retrieval["modes"]["hybrid"]["all"][name], rule)
    return {"passed": not failures, "failures": failures, "thresholds_version": thresholds["version"]}


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run(provider_kind: str = "none", model: str | None = None, base_url: str | None = None,
        write: bool = True, database: Path | None = None) -> dict[str, Any]:
    provider = None
    if provider_kind != "none":
        provider = OpenAICompatibleProvider(base_url or "http://127.0.0.1:11434/v1", model or "qwen2.5:1.5b",
                                            provider_kind, timeout_s=120)
    with closing(build_runtime(":memory:", provider=provider, enable_model=True)) as rt:
        cases = [{**c, "split": "dev"} for c in load_jsonl(DATASETS / "agent_dev_v1.jsonl")]
        cases += [{**c, "split": "heldout"} for c in load_jsonl(DATASETS / "agent_heldout_v1.jsonl")]
        records = [score_case(rt, case) for case in cases]
        mode = "llm" if provider else "deterministic"
        summary = {split: aggregate([r for r in records if split == "all" or r["split"] == split], provider)
                   for split in ("dev", "heldout", "all")}
        retrieval = evaluate_retrieval(rt)
        gate = check_gate(summary["all"], retrieval, mode)
        report = {
            "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "git_commit": git_commit(),
            "dataset_version": DATASET_VERSION,
            "mode": mode,
            "provider": provider.describe() if provider else None,
            "readmission_model_loaded": rt.model_available,
            "environment": {"python": platform.python_version(), "platform": platform.system()},
            "scoring": "deterministic rule-based checks against versioned synthetic cases (no LLM judge, no human "
                       "ratings)",
            "summary": summary,
            "retrieval": retrieval,
            "gate": gate,
            "cases": records,
        }
        if write:
            RESULTS.mkdir(parents=True, exist_ok=True)
            name = "latest.json" if mode == "deterministic" else f"llm_{(model or 'model').replace(':', '_')}.json"
            (RESULTS / name).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
            with (RESULTS / "history.jsonl").open("a", encoding="utf-8") as history:
                history.write(json.dumps({
                    "run_at": report["run_at"], "git_commit": report["git_commit"], "mode": mode,
                    "model": provider.model if provider else None, "gate_passed": gate["passed"],
                    "outcome_accuracy": summary["all"]["outcome_accuracy"],
                    "answer_correctness": summary["all"]["answer_correctness"],
                    "safety_adherence": summary["all"]["safety_adherence"],
                    "unsupported_claim_rate": summary["all"]["unsupported_claim_rate"],
                    "hybrid_recall_at_5": retrieval["modes"]["hybrid"]["all"]["recall_at_5"],
                    "latency_p50_ms": summary["all"]["latency_p50_ms"],
                }) + "\n")
        if database is not None:
            from src.persistence.db import Database
            from src.persistence.metadata import record_evaluation

            with closing(Database(database)) as metadata_db:
                record_evaluation(metadata_db, report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="none", choices=["none", "ollama", "openai-compatible"])
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--database", type=Path, help="optional SQLite destination for aggregate evaluation metadata")
    args = parser.parse_args()
    report = run(args.provider, args.model, args.base_url, write=not args.no_write, database=args.database)
    overall = report["summary"]["all"]
    print(json.dumps({k: overall[k] for k in ("cases", "passed", "outcome_accuracy", "answer_correctness",
                                              "tool_selection_correctness", "safety_adherence",
                                              "unsupported_claim_rate", "isolation_violations",
                                              "latency_p50_ms", "latency_p95_ms")}, indent=1))
    print(json.dumps(report["retrieval"]["modes"]["hybrid"]["all"], indent=1))
    for record in report["cases"]:
        if not record["passed"]:
            print("FAILED", record["id"], record.get("failed_checks"), record.get("answer_excerpt", "")[:160])
    print("GATE", "PASSED" if report["gate"]["passed"] else "FAILED", report["gate"]["failures"])
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
