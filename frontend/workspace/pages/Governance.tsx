import { useState } from "react";
import { ApiError, api } from "../api";
import { EvidenceDrawer } from "../components/EvidencePanel";
import { Badge, Card, EmptyState, ErrorState, REVIEW_TONE, Skeleton, Tabs, formatDate, humanize } from "../components/ui";
import { useApi, useApp } from "../state";
import type { Evidence, Review } from "../types";

type Tab = "reviews" | "access" | "audit";

function ReviewDetail({ reviewId, onChanged }: { reviewId: string; onChanged: () => void }) {
  const { persona } = useApp();
  const review = useApi(() => api.review(reviewId), `review:${reviewId}`);
  const runId = review.data?.artifact?.content.run_id ?? "";
  const run = useApi(() => api.run(runId), `run:${runId}`, Boolean(runId));
  const [comment, setComment] = useState("");
  const [remove, setRemove] = useState<number[]>([]);
  const [message, setMessage] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [open, setOpen] = useState<Evidence | null>(null);
  const r = review.data;
  const isReviewer = persona === "reviewer.demo";
  const isSubmitter = r?.submitted_by === persona;
  const open_ = r && (r.status === "pending_review" || r.status === "needs_revision");

  async function act(fn: () => Promise<Review>, success: string) {
    setMessage(null);
    try {
      await fn();
      setMessage({ tone: "success", text: success });
      setComment("");
      setRemove([]);
      review.reload();
      onChanged();
    } catch (err) {
      setMessage({ tone: "danger", text: err instanceof ApiError ? err.message : "Action failed." });
    }
  }

  if (review.loading) return <Card><Skeleton lines={6} /></Card>;
  if (review.error) return <Card><ErrorState error={review.error} onRetry={review.reload} /></Card>;
  if (!r) return null;
  const evidence = new Map((run.data?.evidence ?? []).map((e) => [e.evidence_id, e]));
  return (
    <Card
      title={r.artifact?.content.question ?? "Review"}
      subtitle={`${r.patient_id} · artifact v${r.artifact_version} · hash ${r.artifact_hash.slice(0, 12)}… · submitted by ${r.submitted_by}`}
      actions={<Badge tone={REVIEW_TONE[r.status] ?? "neutral"} dot>{humanize(r.status)}</Badge>}
    >
      <div className="stack">
        {r.status === "approved" && (
          <div className={`banner ${r.approval_valid ? "success" : "warning"}`}>
            {r.approval_valid ? "Approval is valid for the exact artifact version shown." : "Approval no longer matches the current artifact."}
          </div>
        )}
        {!r.content_visible && <div className="banner info">Artifact content is hidden for this persona (no patient-data permission).</div>}
        {r.artifact && (
          <ol className="claims">
            {r.artifact.content.claims.map((claim, index) => (
              <li className="claim" key={index}>
                {isSubmitter && open_ ? (
                  <input
                    type="checkbox"
                    aria-label={`Remove claim ${index + 1} in a revision`}
                    checked={remove.includes(index)}
                    onChange={(e) => setRemove((rm) => (e.target.checked ? [...rm, index] : rm.filter((x) => x !== index)))}
                  />
                ) : (
                  <span className="claim-index" aria-hidden="true">{index + 1}</span>
                )}
                <div className="claim-body">
                  {claim.text}
                  <div className="citations">
                    {claim.citations.map((c) => (
                      <button key={c} type="button" className="citation" disabled={!evidence.get(c)} onClick={() => { const ev = evidence.get(c); if (ev) setOpen(ev); }}>
                        {c}
                      </button>
                    ))}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}
        {r.artifact?.content.reviewer_note && <p className="muted" style={{ fontSize: 13 }}>Submitter note: {r.artifact.content.reviewer_note}</p>}
        {isReviewer && open_ && (
          <div className="stack">
            <label className="field">
              <span className="field-label">Reviewer comment (required to reject or request revision)</span>
              <textarea className="textarea" value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} />
            </label>
            <div className="row">
              <button type="button" className="btn success" onClick={() => act(() => api.decide(r.review_id, { decision: "approved", artifact_hash: r.artifact_hash, comment }), "Approved this artifact version.")}>Approve</button>
              <button type="button" className="btn" onClick={() => act(() => api.decide(r.review_id, { decision: "needs_revision", artifact_hash: r.artifact_hash, comment }), "Requested revision.")}>Needs revision</button>
              <button type="button" className="btn danger" onClick={() => act(() => api.decide(r.review_id, { decision: "rejected", artifact_hash: r.artifact_hash, comment }), "Rejected.")}>Reject</button>
            </div>
            <p className="faint" style={{ fontSize: 12 }}>The decision is bound to hash {r.artifact_hash.slice(0, 12)}…; if the artifact changes first, the server rejects it.</p>
          </div>
        )}
        {isSubmitter && (open_ || r.status === "approved") && (
          <div className="row">
            <button
              type="button"
              className="btn"
              disabled={remove.length === 0}
              onClick={() => act(() => api.revise(r.review_id, { remove_claims: remove, note: "Removed claims during revision" }), "Created a new artifact version; earlier decisions are superseded.")}
            >
              Revise: remove {remove.length} selected claim{remove.length === 1 ? "" : "s"}
            </button>
            <span className="faint" style={{ fontSize: 12 }}>Any revision creates a new version and invalidates previous approval.</span>
          </div>
        )}
        {!isReviewer && !isSubmitter && open_ && <p className="faint" style={{ fontSize: 13 }}>Switch to the Demo Reviewer persona to decide on this review.</p>}
        {message && <div className={`banner ${message.tone}`} role="status">{message.text}</div>}
        <div>
          <div className="field-label" style={{ marginBottom: 6 }}>Audit history</div>
          <div className="steps">
            {(r.events ?? []).map((e, i) => (
              <div className="step" key={i}>
                <span className="step-status skip" aria-hidden="true" />
                <span style={{ fontSize: 13 }}>
                  <strong>{e.actor}</strong> {e.action} → {humanize(e.to_status)} {e.comment ? `— “${e.comment}”` : ""}
                  <span className="faint mono"> · {e.artifact_hash.slice(0, 8)}</span>
                </span>
                <span className="faint nowrap">{formatDate(e.created_at, true)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      {open && run.data && <EvidenceDrawer evidence={open} patientId={run.data.patient_id} onClose={() => setOpen(null)} />}
    </Card>
  );
}

function ReviewQueue() {
  const reviews = useApi(api.reviews, "reviews");
  const [selected, setSelected] = useState<string | null>(null);
  const active = selected ?? reviews.data?.[0]?.review_id ?? null;
  return (
    <div className="split">
      <Card title="Review queue" subtitle="Agent outputs submitted for human review" flush>
        {reviews.loading && <div className="card-body"><Skeleton /></div>}
        {reviews.error && <ErrorState error={reviews.error} onRetry={reviews.reload} />}
        {reviews.data?.length === 0 && <EmptyState title="Nothing submitted yet">Answer a question as a clinician and choose “Submit for human review”.</EmptyState>}
        <div className="list">
          {(reviews.data ?? []).map((r) => (
            <button key={r.review_id} type="button" className="list-item" aria-current={r.review_id === active} onClick={() => setSelected(r.review_id)}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="item-title mono" style={{ fontSize: 13 }}>{r.review_id.slice(0, 8)} · v{r.artifact_version}</div>
                <div className="item-meta">{r.patient_id} · {formatDate(r.updated_at, true)}</div>
              </div>
              <Badge tone={REVIEW_TONE[r.status] ?? "neutral"}>{humanize(r.status)}</Badge>
            </button>
          ))}
        </div>
      </Card>
      <div style={{ minWidth: 0 }}>{active && <ReviewDetail key={active} reviewId={active} onChanged={reviews.reload} />}</div>
    </div>
  );
}

function AccessControl() {
  const matrix = useApi(api.policyMatrix, "policy");
  const personas = useApi(api.personas, "personas");
  if (matrix.loading) return <Card><Skeleton lines={6} /></Card>;
  if (matrix.error) return <Card><ErrorState error={matrix.error} onRetry={matrix.reload} /></Card>;
  const m = matrix.data;
  if (!m) return null;
  const actions = [...new Set(m.rows.map((r) => r.action))];
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <div className="grid cols-4">
        {(personas.data ?? []).map((p) => (
          <div key={p.persona_id} className="card"><div className="card-body stack" style={{ gap: 4 }}>
            <span className="item-title">{p.display_name}</span>
            <Badge tone="outline">{p.role}</Badge>
            <span className="muted" style={{ fontSize: 13 }}>{p.description}</span>
          </div></div>
        ))}
      </div>
      <Card title="Live policy decisions" subtitle={`${m.note} Patient-scoped actions show one cell per synthetic patient (p001–p004).`} flush>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Action</th>{(personas.data ?? []).map((p) => <th key={p.persona_id}>{p.display_name}</th>)}</tr></thead>
            <tbody>
              {actions.map((action) => (
                <tr key={action}>
                  <td className="mono">{action}</td>
                  {(personas.data ?? []).map((p) => {
                    const row = m.rows.find((r) => r.action === action && r.persona === p.persona_id);
                    return (
                      <td key={p.persona_id}>
                        <span className="row" style={{ gap: 3 }}>
                          {Object.entries(row?.decisions ?? {}).map(([pid, allowed]) => (
                            <span key={pid} className={`matrix-cell ${allowed ? "allow" : "deny"}`} title={`${pid}: ${allowed ? "allow" : "deny"}`}>
                              {allowed ? "✓" : "·"}
                            </span>
                          ))}
                        </span>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <p className="faint" style={{ fontSize: 12 }}>
        Demo personas are synthetic access contexts selected without login. This demonstrates enforcement design; it is not authentication and makes no compliance claim.
      </p>
    </div>
  );
}

function AuditLog() {
  const { persona, setPersona } = useApp();
  const [filter, setFilter] = useState<"" | "deny">("");
  const audit = useApi(() => api.audit(filter || undefined), `audit:${filter}`);
  if (audit.error?.status === 403) {
    return (
      <Card>
        <EmptyState title="The audit log is limited to the administrator persona" action={persona !== "admin.demo" && <button type="button" className="btn primary sm" onClick={() => setPersona("admin.demo")}>Switch to Demo Administrator</button>} />
      </Card>
    );
  }
  return (
    <Card
      title="Audit events"
      subtitle={audit.data ? `${audit.data.counts.allow ?? 0} allowed · ${audit.data.counts.deny ?? 0} denied (identifiers only, no record text)` : undefined}
      actions={<button type="button" className="btn sm" onClick={() => setFilter(filter ? "" : "deny")}>{filter ? "Show all" : "Denials only"}</button>}
      flush
    >
      {audit.loading && <div className="card-body"><Skeleton /></div>}
      {audit.error && <ErrorState error={audit.error} onRetry={audit.reload} />}
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Time</th><th>Persona</th><th>Action</th><th>Resource</th><th>Decision</th><th>Reason</th></tr></thead>
          <tbody>
            {(audit.data?.events ?? []).map((e) => (
              <tr key={e.event_id}>
                <td className="nowrap">{formatDate(e.ts, true)}</td>
                <td className="mono">{e.persona}</td>
                <td className="mono">{e.action}</td>
                <td className="mono">{e.resource}</td>
                <td><Badge tone={e.decision === "allow" ? "success" : "danger"}>{e.decision}</Badge></td>
                <td className="muted">{e.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function GovernancePage() {
  const [tab, setTab] = useState<Tab>("reviews");
  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="page-title">Governance & Review</h1>
          <p className="page-subtitle">Human review bound to exact artifact versions, default-deny access control, and an identifier-only audit trail.</p>
        </div>
      </div>
      <Tabs<Tab> label="Governance sections" value={tab} onChange={setTab} tabs={[{ value: "reviews", label: "Review queue" }, { value: "access", label: "Access control" }, { value: "audit", label: "Audit log" }]} />
      {tab === "reviews" && <ReviewQueue />}
      {tab === "access" && <AccessControl />}
      {tab === "audit" && <AuditLog />}
    </>
  );
}
