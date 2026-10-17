// Renders one agent run: verified claims with citations, uncertainty, tool activity, and review actions.
import { useState } from "react";
import { ApiError, api } from "../api";
import type { AgentRun, Evidence } from "../types";
import { EvidenceDrawer } from "./EvidencePanel";
import { Icons } from "./icons";
import { Badge, OUTCOME_TONE, humanize, ms } from "./ui";

function ModeBadge({ run }: { run: AgentRun }) {
  if (run.inference.mode === "llm") {
    const model = run.inference.provider?.model ?? "model";
    return (
      <Badge tone="violet" dot title={run.inference.label}>
        Live LLM · {model}
      </Badge>
    );
  }
  return (
    <Badge tone="teal" dot title={run.inference.label}>
      Deterministic · no LLM
    </Badge>
  );
}

export function AnswerCard({ run, canReview = true }: { run: AgentRun; canReview?: boolean }) {
  const [open, setOpen] = useState<Evidence | null>(null);
  const [showTrace, setShowTrace] = useState(false);
  const [reviewState, setReviewState] = useState<{ status: string; message: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const byId = new Map(run.evidence.map((e) => [e.evidence_id, e]));
  const verification = run.verification;

  async function submitForReview() {
    setSubmitting(true);
    try {
      const review = await api.submitReview(run.run_id);
      setReviewState({ status: review.status, message: `Submitted as review ${review.review_id.slice(0, 8)} (artifact v${review.artifact_version}).` });
    } catch (error) {
      setReviewState({ status: "error", message: error instanceof ApiError ? error.message : "Submission failed." });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <article className="card" aria-label={`Answer to: ${run.question}`}>
      <div className="card-body answer">
        <div className="row-between">
          <div className="row">
            <Badge tone={OUTCOME_TONE[run.outcome] ?? "neutral"} dot>
              {humanize(run.outcome)}
            </Badge>
            <ModeBadge run={run} />
            {verification && (
              <Badge tone={verification.status === "verified" ? "success" : verification.status === "failed" ? "danger" : "warning"}>
                {verification.claims_supported}/{verification.claims_total} claims verified
              </Badge>
            )}
            {run.review.required && <Badge tone="warning">Review recommended</Badge>}
          </div>
          <span className="faint nowrap" style={{ fontSize: 12 }}>
            {ms(run.latency_ms)}
          </span>
        </div>

        {run.refusal && (
          <div className="banner danger" role="note">
            <Icons.alert size={18} />
            <div>
              <strong>Not answered ({humanize(run.refusal.category)}).</strong> {run.refusal.message}
            </div>
          </div>
        )}
        {run.answer.abstain_reason && (
          <div className="banner warning" role="note">
            <Icons.alert size={18} />
            <div>
              <strong>No supported answer.</strong> {run.answer.abstain_reason}
            </div>
          </div>
        )}
        {run.answer.summary && <p style={{ fontWeight: 600 }}>{run.answer.summary}</p>}

        {run.answer.claims.length > 0 && (
          <ol className="claims" aria-label="Verified claims">
            {run.answer.claims.map((claim, index) => (
              <li className="claim" key={`${index}-${claim.text.slice(0, 20)}`}>
                <span className="claim-index" aria-hidden="true">{index + 1}</span>
                <div className="claim-body">
                  {claim.text}
                  <div className="citations">
                    {claim.citations.map((citation) => {
                      const evidence = byId.get(citation);
                      return (
                        <button
                          key={citation}
                          type="button"
                          className="citation"
                          onClick={() => evidence && setOpen(evidence)}
                          disabled={!evidence}
                          aria-label={`Open evidence ${citation}`}
                          title={evidence?.title}
                        >
                          {citation}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}

        {run.risk && (
          <div className={`banner ${run.risk.available ? "info" : "warning"}`}>
            <div>
              {run.risk.available ? (
                <>
                  <strong>Readmission model:</strong> probability {((run.risk.risk_probability ?? 0) * 100).toFixed(1)}% (
                  {run.risk.risk_label} at threshold {((run.risk.risk_threshold ?? 0) * 100).toFixed(0)}%) for encounter{" "}
                  <span className="mono">{run.risk.encounter_id}</span>, model <span className="mono">{run.risk.model_version}</span>.
                  Out-of-distribution estimate; see caveats.
                </>
              ) : (
                <>
                  <strong>Readmission model unavailable for this record.</strong> {run.risk.reasons?.join(" ")}
                </>
              )}
            </div>
          </div>
        )}

        {run.answer.uncertainty.length > 0 && (
          <details>
            <summary className="muted" style={{ cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
              Uncertainty and caveats ({run.answer.uncertainty.length})
            </summary>
            <ul className="muted" style={{ fontSize: 13, margin: "8px 0 0", paddingLeft: 18 }}>
              {run.answer.uncertainty.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </details>
        )}

        {(run.answer.rejected_claims.length > 0 || (run.inference.llm_attempt?.rejected.length ?? 0) > 0) && (
          <details>
            <summary className="muted" style={{ cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
              Claims removed by verification ({run.answer.rejected_claims.length + (run.inference.fallback_used ? run.inference.llm_attempt?.rejected.length ?? 0 : 0)})
            </summary>
            <ol className="claims" style={{ marginTop: 8 }}>
              {[...(run.inference.fallback_used ? run.inference.llm_attempt?.rejected ?? [] : []), ...run.answer.rejected_claims].map((claim, index) => (
                <li className="claim rejected" key={`r-${index}`}>
                  <span className="claim-index" aria-hidden="true">✕</span>
                  <div className="claim-body">
                    <span style={{ textDecoration: "line-through", opacity: 0.8 }}>{claim.text}</span>
                    <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>{(claim.reasons ?? []).join("; ")}</div>
                  </div>
                </li>
              ))}
            </ol>
          </details>
        )}

        <div className="row-between" style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
          <button type="button" className="btn ghost sm" onClick={() => setShowTrace((v) => !v)} aria-expanded={showTrace}>
            {showTrace ? "Hide" : "Show"} agent activity · {run.tool_calls.length} tool call{run.tool_calls.length === 1 ? "" : "s"}
          </button>
          {canReview && run.outcome === "answered" && (
            <button type="button" className="btn sm" onClick={submitForReview} disabled={submitting || reviewState?.status === "pending_review"}>
              {submitting ? "Submitting…" : "Submit for human review"}
            </button>
          )}
        </div>
        {reviewState && (
          <div className={`banner ${reviewState.status === "error" ? "danger" : "success"}`} role="status">
            {reviewState.message}
          </div>
        )}
        {showTrace && (
          <div className="grid cols-2">
            <div>
              <div className="field-label">Plan</div>
              <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                Planner: <span className="mono">{run.inference.planner ?? "—"}</span> · Composer: <span className="mono">{run.inference.composer ?? "—"}</span>
                {run.plan?.intents.length ? <> · Intents: {run.plan.intents.join(", ")}</> : null}
              </p>
              {run.plan?.notes.map((note) => (
                <p key={note} className="faint" style={{ fontSize: 12 }}>{note}</p>
              ))}
              <div className="steps" style={{ marginTop: 8 }}>
                {run.tool_calls.map((call) => (
                  <div className="step" key={call.tool}>
                    <span className={`step-status ${call.ok ? "" : "fail"}`} aria-label={call.ok ? "succeeded" : "failed"} />
                    <span className="mono" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                      {call.tool}
                      {call.error_type && <span className="faint"> · {call.error_type}</span>}
                    </span>
                    <span className="faint nowrap">
                      {call.evidence_count} ev · {ms(call.latency_ms)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
            <div>
              <div className="field-label">Graph trace</div>
              <div className="steps" style={{ marginTop: 8 }}>
                {run.trace.map((span, index) => (
                  <div className="step" key={`${span.node}-${index}`}>
                    <span className="step-status skip" aria-hidden="true" />
                    <span className="mono">{span.node}{typeof span.tool === "string" ? ` · ${span.tool}` : ""}</span>
                    <span className="faint nowrap">{ms(span.duration_ms)}</span>
                  </div>
                ))}
              </div>
              <p className="faint" style={{ fontSize: 12, marginTop: 8 }}>
                Run <span className="mono">{run.run_id.slice(0, 8)}</span> · session <span className="mono">{run.session_id.slice(0, 8)}</span>. Traces hold identifiers and timings only, never record text.
              </p>
            </div>
          </div>
        )}
      </div>
      {open && <EvidenceDrawer evidence={open} patientId={run.patient_id} onClose={() => setOpen(null)} />}
    </article>
  );
}
