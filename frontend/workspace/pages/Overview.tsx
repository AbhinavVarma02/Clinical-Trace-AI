import { useState } from "react";
import { api, getWorkspaceId, resetWorkspaceId } from "../api";
import { GuidedCase } from "../components/Availability";
import { EvidenceSnapshot } from "../components/EvidenceSnapshot";
import { Icons } from "../components/icons";
import { Badge, Card, EmptyState, ErrorState, OUTCOME_TONE, Skeleton, Stat, formatDate, humanize, ms, pct } from "../components/ui";
import { DEMOS } from "../demos";
import { reloadGuide, resetGuide } from "../guide";
import { navigate, useApi, useApp } from "../state";

export function OverviewPage() {
  const { setPatientId } = useApp();
  const status = useApi(api.status, "status");
  const patients = useApi(api.patients, "patients");
  const recent = useApi(api.recentRuns, "recent");
  const s = status.data;
  const [workspace, setWorkspace] = useState(getWorkspaceId);

  function newWorkspace() {
    setWorkspace(resetWorkspaceId());
    resetGuide();
    reloadGuide();
    recent.reload();
  }

  function startDemo(patientId: string, question: string) {
    setPatientId(patientId);
    navigate("agent", { patient: patientId, q: question, run: "1" });
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="page-title">Clinical-Trace AI 2.0</h1>
          <p className="page-subtitle">
            Evidence-grounded clinical intelligence over synthetic FHIR records: a tool-using review agent whose every statement is verified
            against cited sources, with explainable readmission risk, human review, and measured quality.
          </p>
        </div>
      </div>

      <GuidedCase />

      <div className="banner warning" role="note">
        <Icons.alert size={18} />
        <div>
          <strong>Synthetic, educational prototype.</strong> All patients are generated fixtures. Outputs are decision-support demonstrations, not medical
          advice, and the system is not clinically validated.
        </div>
      </div>

      <div className="workspace-chip">
        <span>Private demo workspace <span className="mono">{workspace.slice(0, 8)}</span>: your runs, reviews and audit events are visible only in this browser. No sign-in, no personal data.</span>
        <button type="button" className="btn ghost sm" onClick={newWorkspace}>Start fresh</button>
      </div>

      {status.error && <ErrorState error={status.error} onRetry={status.reload} />}
      {status.loading && !s && <Skeleton lines={4} />}
      {s && (
        <div className="grid cols-4">
          <Stat label="Service" value={<span style={{ textTransform: "capitalize" }}>{s.status}</span>} tone="success" note={`v${s.version} · schema v${s.database.schema_version}`} />
          <Stat label="Synthetic patients" value={s.data.patients} note={`${s.retrieval.chunks} indexed chunks · ${s.data.startup_ingestion.invalid} invalid`} />
          <Stat
            label="Inference"
            value={s.inference.mode === "llm" ? "Live LLM" : "Deterministic"}
            tone={s.inference.mode === "llm" ? "violet" : "teal"}
            note={s.inference.mode === "llm" ? `${s.inference.provider?.provider} · ${s.inference.provider?.model}` : "Rule-based planning, extractive answers"}
          />
          <Stat
            label="Evaluation gate"
            value={s.evaluation ? (s.evaluation.gate_passed ? "Passed" : "Failed") : "Not run"}
            tone={s.evaluation ? (s.evaluation.gate_passed ? "success" : "danger") : undefined}
            note={s.evaluation ? `${s.evaluation.mode} · ${formatDate(s.evaluation.run_at)}` : "Run python -m evaluation.run_eval"}
          />
        </div>
      )}

      <Card title="Guided scenarios" subtitle="Each runs the real agent on a synthetic patient. Results are produced live, not replayed.">
        <div className="grid cols-4">
          {DEMOS.map((demo) => (
            <button key={demo.id} type="button" className="card interactive" onClick={() => startDemo(demo.patientId, demo.question)}>
              <div className="card-body stack">
                <div className="row-between">
                  <span className="item-title">{demo.title}</span>
                  <Icons.arrow size={16} />
                </div>
                <span className="mono faint" style={{ fontSize: 12 }}>{demo.patientId}</span>
                <span className="muted" style={{ fontSize: 13 }}>{demo.shows}</span>
                <span className="faint" style={{ fontSize: 12 }}>“{demo.question}”</span>
              </div>
            </button>
          ))}
        </div>
      </Card>

      <EvidenceSnapshot />

      <div className="split-wide">
        <Card title="Recent analysis sessions" subtitle="Runs made with the current persona in this workspace" flush>
          {recent.loading && <div className="card-body"><Skeleton /></div>}
          {recent.error && <ErrorState error={recent.error} onRetry={recent.reload} />}
          {recent.data && recent.data.length === 0 && (
            <EmptyState title="No runs yet">Start a guided scenario or ask a question in the agent workspace.</EmptyState>
          )}
          {recent.data && recent.data.length > 0 && (
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
          )}
        </Card>

        <div className="stack">
          <Card title="Readmission model" subtitle="UCI Diabetes 130-US benchmark (held-out test)">
            {s?.readmission_model.loaded && s.readmission_model.metadata ? (
              <dl className="kv">
                <dt>Model</dt>
                <dd>{s.readmission_model.metadata.model_type}</dd>
                <dt>Version</dt>
                <dd className="mono">{s.readmission_model.metadata.model_version}</dd>
                <dt>ROC-AUC</dt>
                <dd>{s.readmission_model.metadata.test_metrics.roc_auc?.toFixed(3)}</dd>
                <dt>PR-AUC</dt>
                <dd>{s.readmission_model.metadata.test_metrics.pr_auc?.toFixed(3)}</dd>
                <dt>Recall / precision</dt>
                <dd>
                  {pct(s.readmission_model.metadata.test_metrics.recall_positive)} / {pct(s.readmission_model.metadata.test_metrics.precision_positive)}
                </dd>
              </dl>
            ) : !s ? (
              <p className="muted" style={{ fontSize: 13 }}>Model status appears when the backend is reachable. Held-out metrics are listed under Measured evidence.</p>
            ) : (
              <p className="muted" style={{ fontSize: 13 }}>
                Model artifacts are not loaded on this server, so risk tools report the model as unavailable. Train with <span className="mono">python src/train.py</span>.
              </p>
            )}
          </Card>
          <Card title="Synthetic patients" flush>
            {patients.error && <ErrorState error={patients.error} onRetry={patients.reload} />}
            <div className="list">
              {(patients.data ?? []).map((p) => (
                <button
                  key={p.patient_id}
                  type="button"
                  className="list-item"
                  onClick={() => {
                    setPatientId(p.patient_id);
                    navigate("patients");
                  }}
                >
                  <span className="avatar" aria-hidden="true">{p.display_name.slice(0, 1)}</span>
                  <div style={{ minWidth: 0 }}>
                    <div className="item-title">{p.display_name.replace(" (synthetic record)", "")}</div>
                    <div className="item-meta">{p.conditions.slice(0, 2).join(" · ") || "No conditions recorded"}</div>
                  </div>
                </button>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}
