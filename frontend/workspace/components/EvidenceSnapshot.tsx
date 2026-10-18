// Measured results copied from committed evaluation artifacts (evidence-snapshot.json). Rendered
// without the backend so a first-time visitor sees real evidence while the backend starts.
// Headline numbers stay visible; the full measurements and limits are one click away.
import snapshot from "../evidence-snapshot.json";
import { Badge, Card, Disclosure, ms, pct } from "./ui";

export function EvidenceSnapshot() {
  const r = snapshot.readmission;
  const [deterministic, ...llmRuns] = snapshot.agent_evaluations;
  const live = llmRuns[0];
  const isolationClean = snapshot.agent_evaluations.every((run) => run.isolation_violations === 0);
  return (
    <Card
      title="Measured results"
      subtitle="From versioned evaluation runs on synthetic cases"
      actions={<a className="btn ghost sm" href="#/evaluation">Full report</a>}
    >
      <div className="stack" style={{ gap: "var(--space-4)" }}>
        <div className="tile-grid">
          {deterministic && (
            <div className="tile">
              <div className="stat-label">Agent test cases</div>
              <div className="stat-value">{deterministic.passed}/{deterministic.cases}</div>
              <div className="stat-note">passed · deterministic mode</div>
            </div>
          )}
          {live && (
            <div className="tile">
              <div className="stat-label">With a live LLM</div>
              <div className="stat-value">{live.passed}/{live.cases}</div>
              <div className="stat-note">passed · median {ms(live.latency_p50_ms)}</div>
            </div>
          )}
          <div className="tile">
            <div className="stat-label">Cross-patient leaks</div>
            <div className="stat-value">{isolationClean ? "0" : "See report"}</div>
            <div className="stat-note">isolation violations in every run</div>
          </div>
          <div className="tile">
            <div className="stat-label">Finding the right record</div>
            <div className="stat-value">{pct(snapshot.retrieval.recall_at_5)}</div>
            <div className="stat-note">recall@5 over {snapshot.retrieval.queries} queries</div>
          </div>
        </div>
        <p className="muted" style={{ fontSize: 13, margin: 0 }}>
          <strong style={{ color: "var(--text)" }}>Known limit:</strong> the readmission risk score is poorly calibrated. It must not be read as a patient-level probability.
        </p>
        <Disclosure label="All measurements and limits">
          <div className="evidence-grid">
            {snapshot.agent_evaluations.map((run) => (
              <div key={run.label} className="evidence-item">
                <div className="stat-label">{run.label}</div>
                <div className="stat-value">{run.passed}/{run.cases} cases</div>
                <div className="row" style={{ flexWrap: "wrap", gap: 6, margin: "6px 0" }}>
                  <Badge tone={run.gate_passed ? "success" : "danger"}>Gate {run.gate_passed ? "passed" : "failed"}</Badge>
                  <Badge tone="outline">{run.isolation_violations} isolation violations</Badge>
                </div>
                <div className="stat-note">
                  Correctness {pct(run.answer_correctness)} · unsupported claims {pct(run.unsupported_claim_rate)}<br />
                  p50 {ms(run.latency_p50_ms)} · p95 {ms(run.latency_p95_ms)} · {run.where}
                </div>
              </div>
            ))}
            <div className="evidence-item">
              <div className="stat-label">Hybrid retrieval ({snapshot.retrieval.queries} queries)</div>
              <div className="stat-value">{pct(snapshot.retrieval.recall_at_5)} recall@5</div>
              <div className="stat-note">MRR@10 {snapshot.retrieval.mrr_at_10.toFixed(3)} · nDCG@10 {snapshot.retrieval.ndcg_at_10.toFixed(3)}</div>
            </div>
            <div className="evidence-item">
              <div className="stat-label">Readmission model (held-out test, {r.test_rows?.toLocaleString()} rows)</div>
              <div className="stat-value">ROC-AUC {r.roc_auc.toFixed(3)}</div>
              <div className="stat-note">
                PR-AUC {r.pr_auc.toFixed(3)} · Brier {r.brier.toFixed(3)} · ECE {r.ece.toFixed(3)}<br />
                Mean score {pct(r.mean_probability)} vs observed rate {pct(r.prevalence)}
              </div>
            </div>
          </div>
          <div className="banner warning" role="note">
            <div>
              <strong>Limits.</strong> {r.warning} {snapshot.note}
            </div>
          </div>
        </Disclosure>
      </div>
    </Card>
  );
}
