import { useState } from "react";
import { api } from "../api";
import { Badge, Card, Disclosure, ErrorState, Segmented, Skeleton, Stat, formatDate, humanize, ms, pct } from "../components/ui";
import { useApi } from "../state";
import type { EvalSummary } from "../types";

type Split = "all" | "dev" | "heldout";

const METRICS: { key: keyof EvalSummary; label: string; kind: "pct" | "count" | "ms" }[] = [
  { key: "outcome_accuracy", label: "Outcome accuracy", kind: "pct" },
  { key: "answer_correctness", label: "Answer correctness", kind: "pct" },
  { key: "tool_selection_correctness", label: "Tool selection", kind: "pct" },
  { key: "evidence_recall", label: "Evidence recall", kind: "pct" },
  { key: "citation_coverage", label: "Citation coverage", kind: "pct" },
  { key: "citation_precision", label: "Citation precision", kind: "pct" },
  { key: "groundedness", label: "Groundedness (verified share)", kind: "pct" },
  { key: "unsupported_claim_rate", label: "Unsupported-claim rate", kind: "pct" },
  { key: "safety_adherence", label: "Safety adherence (critical)", kind: "pct" },
  { key: "isolation_violations", label: "Isolation violations", kind: "count" },
  { key: "authorization_violations", label: "Authorization violations", kind: "count" },
  { key: "fallback_rate", label: "LLM fallback rate", kind: "pct" },
  { key: "latency_p50_ms", label: "Latency p50", kind: "ms" },
  { key: "latency_p95_ms", label: "Latency p95", kind: "ms" },
];

function fmt(value: unknown, kind: "pct" | "count" | "ms"): string {
  if (typeof value !== "number") return "—";
  if (kind === "pct") return pct(value);
  if (kind === "ms") return ms(value);
  return String(value);
}

export function EvaluationPage() {
  const report = useApi(api.evaluation, "evaluation");
  const [split, setSplit] = useState<Split>("all");
  const r = report.data;
  const columns = r ? [{ name: "Deterministic", summary: r.summary, gate: r.gate }, ...r.comparisons.map((c) => ({ name: c.provider?.model ?? c.file, summary: c.summary, gate: c.gate }))] : [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="page-title">Evaluation</h1>
          <p className="page-subtitle">
            Rule-based scoring on versioned synthetic cases, with thresholds fixed in advance. No LLM judge. Passing does not prove clinical safety.
          </p>
        </div>
        <Segmented<Split> label="Case split" value={split} onChange={setSplit} options={[{ value: "all", label: "All" }, { value: "dev", label: "Development" }, { value: "heldout", label: "Held-out" }]} />
      </div>
      {report.loading && <Card><Skeleton lines={6} /></Card>}
      {report.error && <Card><ErrorState error={report.error} onRetry={report.reload} /></Card>}
      {r && (
        <>
          <div className="grid cols-4">
            <Stat label="Regression gate" value={r.gate.passed ? "Passed" : "Failed"} tone={r.gate.passed ? "success" : "danger"} note={`thresholds v${r.gate.thresholds_version}`} />
            <Stat label="Cases passed" value={`${r.summary[split].passed}/${r.summary[split].cases}`} note={`dataset ${r.dataset_version} · ${split}`} />
            <Stat label="Hybrid recall@5" value={pct(r.retrieval.modes.hybrid?.[split]?.recall_at_5)} note={`MRR@10 ${r.retrieval.modes.hybrid?.[split]?.mrr_at_10?.toFixed(3) ?? "—"}`} />
            <Stat label="Retrieval isolation" value={`${r.retrieval.isolation.violations} violations`} tone={r.retrieval.isolation.violations === 0 ? "success" : "danger"} note={`${r.retrieval.isolation.probes} cross-patient probes`} />
          </div>
          {r.gate.failures.length > 0 && (
            <div className="banner danger"><div><strong>Gate failures:</strong> {r.gate.failures.join("; ")}</div></div>
          )}
          <Card title="Agent quality by inference mode" subtitle={`Latest deterministic run ${formatDate(r.run_at, true)} · commit ${r.git_commit ?? "unknown"}. LLM columns are separate recorded runs.`} flush>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Metric ({split})</th>
                    {columns.map((c) => <th key={c.name} className="num">{c.name}</th>)}
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Gate</td>
                    {columns.map((c) => <td key={c.name} className="num"><Badge tone={c.gate?.passed ? "success" : "danger"}>{c.gate?.passed ? "pass" : "fail"}</Badge></td>)}
                  </tr>
                  {METRICS.map((m) => (
                    <tr key={m.key}>
                      <td>{m.label}</td>
                      {columns.map((c) => <td key={c.name} className="num mono">{fmt(c.summary[split]?.[m.key], m.kind)}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <div className="stack" style={{ gap: "var(--space-6)" }}>
            <Card title="Retrieval benchmark" subtitle={`${r.retrieval.modes.hybrid?.[split]?.queries ?? 0} queries · embedder ${r.retrieval.embedder}`} flush>
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Mode</th><th className="num">Recall@5</th><th className="num">Precision@5</th><th className="num">MRR@10</th><th className="num">nDCG@10</th><th className="num">p95</th></tr></thead>
                  <tbody>
                    {Object.entries(r.retrieval.modes).map(([mode, splits]) => (
                      <tr key={mode}>
                        <td>{mode}</td>
                        <td className="num mono">{pct(splits[split]?.recall_at_5)}</td>
                        <td className="num mono">{pct(splits[split]?.precision_at_5)}</td>
                        <td className="num mono">{splits[split]?.mrr_at_10?.toFixed(3) ?? "—"}</td>
                        <td className="num mono">{splits[split]?.ndcg_at_10?.toFixed(3) ?? "—"}</td>
                        <td className="num mono">{ms(splits[split]?.latency_p95_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
            <Card title="Failed cases" subtitle="Reported as measured. Held-out cases are never tuned against." flush>
              <div className="list scroll-panel" role="region" aria-label="Failed cases list" tabIndex={0}>
                {columns.flatMap((c, ci) => {
                  const cases = ci === 0 ? r.cases.filter((x) => !x.passed) : r.comparisons[ci - 1]?.failed_cases ?? [];
                  return cases.filter((x) => split === "all" || x.split === split).map((x) => (
                    <div className="list-item" key={`${c.name}-${x.id}`}>
                      <div style={{ minWidth: 0 }}>
                        <div className="row">
                          <span className="mono item-title">{x.id}</span>
                          <Badge>{c.name}</Badge>
                          <Badge tone="outline">{humanize(x.category)}</Badge>
                        </div>
                        <div className="item-meta">Failed: {(x.failed_checks ?? []).join(", ")}</div>
                        {x.answer_excerpt && <div className="faint" style={{ fontSize: 12 }}>{x.answer_excerpt.slice(0, 160)}…</div>}
                      </div>
                    </div>
                  ));
                })}
              </div>
            </Card>
          </div>
          <Disclosure label="Run history" hint={`${r.history.length} recorded runs`}>
          <Card flush>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Run</th><th>Commit</th><th>Mode</th><th>Gate</th><th className="num">Outcome acc.</th><th className="num">Correctness</th><th className="num">Safety</th><th className="num">p50</th></tr></thead>
                <tbody>
                  {[...r.history].reverse().map((h, i) => (
                    <tr key={`${h.run_at}-${i}`}>
                      <td className="nowrap">{formatDate(h.run_at, true)}</td>
                      <td className="mono">{h.git_commit ?? "—"}</td>
                      <td>{h.model ?? h.mode}</td>
                      <td><Badge tone={h.gate_passed ? "success" : "danger"}>{h.gate_passed ? "pass" : "fail"}</Badge></td>
                      <td className="num mono">{pct(h.outcome_accuracy)}</td>
                      <td className="num mono">{pct(h.answer_correctness)}</td>
                      <td className="num mono">{pct(h.safety_adherence)}</td>
                      <td className="num mono">{ms(h.latency_p50_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          </Disclosure>
        </>
      )}
    </>
  );
}
