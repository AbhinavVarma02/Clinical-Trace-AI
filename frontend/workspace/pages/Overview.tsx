import { useState } from "react";
import { api, getWorkspaceId, resetWorkspaceId } from "../api";
import { GuidedCase } from "../components/Availability";
import { EvidenceSnapshot } from "../components/EvidenceSnapshot";
import { Icons } from "../components/icons";
import { Badge, Card, ErrorState, OUTCOME_TONE, Skeleton, formatDate, humanize, ms } from "../components/ui";
import { DEMOS } from "../demos";
import { reloadGuide, resetGuide } from "../guide";
import { useExecutionAllowance } from "../edge";
import { navigate, useApi, useApp } from "../state";

export function OverviewPage() {
  const { setPatientId } = useApp();
  const status = useApi(api.status, "status");
  const recent = useApi(api.recentRuns, "recent");
  const s = status.data;
  const [workspace, setWorkspace] = useState(getWorkspaceId);
  const allowance = useExecutionAllowance();

  function newWorkspace() {
    setWorkspace(resetWorkspaceId());
    resetGuide();
    reloadGuide();
    recent.reload();
  }

  function startDemo(patientId: string, question: string) {
    if (allowance.blocked) return;
    setPatientId(patientId);
    navigate("agent", { patient: patientId, q: question, run: "1" });
  }

  return (
    <>
      <GuidedCase />

      {status.error && <ErrorState error={status.error} onRetry={status.reload} />}
      {status.loading && !s && <Skeleton lines={1} />}
      {s && (
        <div className="status-strip" aria-label="System status">
          <Badge tone="success" dot title={`v${s.version} · schema v${s.database.schema_version}`}>{humanize(s.status)}</Badge>
          <span>{s.data.patients} synthetic patients</span>
          <span title={s.inference.mode === "llm" ? `${s.inference.provider?.provider} · ${s.inference.provider?.model}` : "Rule-based planning, extractive answers"}>
            {s.inference.mode === "llm" ? "Live LLM" : "Deterministic mode"}
          </span>
          {s.evaluation && (
            <>
              <span title={`${s.evaluation.mode} · ${formatDate(s.evaluation.run_at)}`}>Evaluation gate {s.evaluation.gate_passed ? "passed" : "failed"}</span>
            </>
          )}
        </div>
      )}

      <section aria-labelledby="cases-title" className="stack" style={{ gap: "var(--space-3)" }}>
        <div className="section-head">
          <div>
            <h2 id="cases-title" className="section-title">Try another case</h2>
            <p className="section-sub">Each one runs the live agent. Nothing is replayed.</p>
          </div>
        </div>
        <div className="grid cols-4">
          {DEMOS.map((demo) => (
            <button key={demo.id} type="button" className="card interactive scenario" disabled={allowance.blocked} onClick={() => startDemo(demo.patientId, demo.question)}>
              <span className="row-between" style={{ flexWrap: "nowrap" }}>
                <span className="item-title">{demo.title}</span>
                <span className="scenario-go"><Icons.arrow size={16} /></span>
              </span>
              <span className="muted" style={{ fontSize: 13 }}>{demo.shows}</span>
            </button>
          ))}
        </div>
      </section>

      <EvidenceSnapshot />

      {recent.error && <ErrorState error={recent.error} onRetry={recent.reload} />}
      {recent.data && recent.data.length > 0 && (
        <Card title="Recent sessions" flush>
          <div className="list">
            {recent.data.map((run) => (
              <button
                key={run.run_id}
                type="button"
                className="list-item"
                onClick={() => {
                  setPatientId(run.patient_id);
                  navigate("agent", { patient: run.patient_id, open: run.run_id });
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="item-title" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{run.question}</div>
                  <div className="item-meta">
                    <span className="mono">{run.patient_id}</span> · {formatDate(run.created_at, true)} · {ms(run.latency_ms)}
                  </div>
                </div>
                <Badge tone={OUTCOME_TONE[run.outcome] ?? "neutral"}>{humanize(run.outcome)}</Badge>
              </button>
            ))}
          </div>
        </Card>
      )}

      <div className="workspace-chip">
        <span>Private workspace <span className="mono">{workspace.slice(0, 8)}</span> · visible only in this browser · no sign-in</span>
        <button type="button" className="btn ghost sm" onClick={newWorkspace}>Start fresh</button>
      </div>
    </>
  );
}
