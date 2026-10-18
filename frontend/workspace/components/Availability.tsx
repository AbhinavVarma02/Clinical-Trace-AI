// Backend availability, free-quota state and the guided-case UI.
import { useEffect, useState } from "react";
import { formatDuration, useAvailability, useExecutionAllowance } from "../edge";
import { GUIDE_CASE, GUIDE_STEPS, dismissGuide, resetGuide, useGuide } from "../guide";
import { navigate, useApp } from "../state";
import { Icons } from "./icons";
import { Badge } from "./ui";

function useElapsed(since: number | null): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (since === null) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [since]);
  return since === null ? 0 : Math.max(0, (now - since) / 1000);
}

/** Compact top-bar indicator: backend state plus live-inference availability. */
export function AvailabilityPill() {
  const a = useAvailability();
  const allowance = useExecutionAllowance();
  const elapsed = useElapsed(a.waitingSince);
  if (a.mode !== "edge" || !a.status) return null;
  if (a.backend === "ready") {
    if (allowance.blocked) return <Badge tone="warning" dot title={allowance.message}>Agent runs paused</Badge>;
    const quota = a.status.inference.quota_state;
    const live = a.status.backend.inference_mode === "llm";
    return (
      <Badge tone={!live ? "teal" : quota === "exhausted" ? "warning" : "success"} dot
        title={`Backend ready (${a.status.backend.latency_ms ?? "?"} ms). ${live ? `Live inference: ${a.status.backend.inference_model}` : "Deterministic mode"}`}>
        {live ? (quota === "exhausted" ? "Ready · deterministic until reset" : "Ready · live LLM") : "Ready · deterministic"}
      </Badge>
    );
  }
  if (a.backend === "waking") return <Badge tone="warning" dot title="The backend sleeps when idle and is starting">Starting · {formatDuration(elapsed)}</Badge>;
  if (a.backend === "not_configured") return <Badge tone="danger" dot>No backend configured</Badge>;
  return <Badge tone="danger" dot title="The backend is not answering; retrying">Reconnecting · {formatDuration(elapsed)}</Badge>;
}

/** Explains cold starts, outages and free-quota exhaustion where the visitor will see it. */
export function AvailabilityBanner() {
  const a = useAvailability();
  const allowance = useExecutionAllowance();
  const elapsed = useElapsed(a.waitingSince);
  if (allowance.blocked && a.backend !== "not_configured") {
    return <div className="banner warning" role="status"><Icons.alert size={18} /><div>{allowance.message}</div></div>;
  }
  if (a.mode !== "edge") return null;
  if (a.backend === "waking" || a.backend === "unavailable") {
    return (
      <div className="banner info availability-banner" role="status" aria-live="polite">
        <span className="spinner" aria-hidden="true" />
        <div>
          <strong>{a.backend === "waking" ? "Starting the analysis backend" : "Reconnecting to the analysis backend"} · {formatDuration(elapsed)}</strong>
          <div className="muted" style={{ fontSize: 13 }}>
            {a.backend === "waking" ? "Free hosting sleeps when idle, so the agent needs a short start-up."
              : "The connection or service is unavailable. Checking automatically; no agent run is being retried."}
            {elapsed > 120 ? " Still checking." : ""}
          </div>
        </div>
        <button type="button" className="btn sm" onClick={a.checkNow}>Check now</button>
      </div>
    );
  }
  if (!a.status) return null;
  if (a.backend === "not_configured") {
    return <div className="banner danger" role="alert"><Icons.alert size={18} /><div>No analysis backend is connected, so agent features are unavailable.</div></div>;
  }
  const q = a.status.inference;
  if (a.status.backend.inference_mode === "llm" && q.quota_state === "exhausted") {
    return (
      <div className="banner warning" role="status">
        <Icons.alert size={18} />
        <div>
          <strong>Today's free live-AI allocation is used up.</strong> Answers continue in deterministic mode, with the same verification and review, and are labelled.
          Live AI resumes at 00:00 UTC{q.resets_in_s !== null ? ` (in ${formatDuration(q.resets_in_s)})` : ""}.
          {allowance.visitor !== null && ` Your connection has ${allowance.visitor} daily agent-run attempts left.`}
          {allowance.global !== null && ` Deployment-wide: ${allowance.global} remaining.`}
        </div>
      </div>
    );
  }
  return <div className="banner info" role="status"><div>
    {allowance.visitor === null ? "Your connection's remaining agent-run allowance is unavailable."
      : `${allowance.visitor} of ${a.status.limits.agent_runs_per_visitor_per_day} daily agent-run attempts left for your connection.`}
    {allowance.global !== null && ` Deployment-wide: ${allowance.global} remaining.`}
    {" Resets at 00:00 UTC. Visitors sharing a connection share its allowance."}
  </div></div>;
}

function startGuidedCase(setPatientId: (id: string) => void) {
  setPatientId(GUIDE_CASE.patientId);
  navigate("agent", { patient: GUIDE_CASE.patientId, q: GUIDE_CASE.question, run: "1" });
}

/** Overview hero: what this is, why it matters, and the one obvious thing to click. */
export function GuidedCase() {
  const guide = useGuide();
  const { setPatientId, setPersona } = useApp();
  const a = useAvailability();
  const allowance = useExecutionAllowance();
  const waiting = a.mode === "edge" && (a.backend === "waking" || a.backend === "unavailable");
  const noBackend = a.mode === "edge" && a.backend === "not_configured";
  return (
    <section className="card hero" aria-labelledby="hero-title">
      <div className="hero-body">
        <div>
          <span className="eyebrow">Clinical-Trace AI 2.0</span>
          <h1 id="hero-title" className="hero-title"><span className="hero-line">Understand patient records.</span> <span className="hero-line">See the evidence.</span></h1>
        </div>
        <p className="hero-sub">Explore synthetic clinical cases with an AI agent that explains its findings and cites its sources.</p>
        <div className="hero-actions">
          {guide.complete ? (
            <button type="button" className="btn hero-cta" onClick={resetGuide}><Icons.refresh size={16} /> Run it again</button>
          ) : guide.next?.id === "approve" ? (
            <button type="button" className="btn primary hero-cta" onClick={() => { setPersona("reviewer.demo"); navigate("governance"); }}>
              <Icons.play size={16} /> Approve as reviewer
            </button>
          ) : guide.next?.id === "run" ? (
            <button type="button" className="btn primary hero-cta" onClick={() => startGuidedCase(setPatientId)} disabled={noBackend || allowance.blocked}
              aria-describedby={waiting || noBackend ? "guide-wait" : undefined}>
              <Icons.play size={16} /> Try a guided case
            </button>
          ) : (
            <button type="button" className="btn primary hero-cta" onClick={() => navigate("agent", guide.runId
              ? { patient: GUIDE_CASE.patientId, open: guide.runId } : { patient: GUIDE_CASE.patientId })}>
              <Icons.arrow size={16} /> Continue
            </button>
          )}
          <span className="hero-meta">About 90 seconds · runs the real agent</span>
        </div>
        {(waiting || noBackend) && guide.next?.id === "run" && (
          <p id="guide-wait" className="hero-meta" style={{ margin: 0 }}>
            {noBackend ? "No backend is connected, so the guided case cannot run here. Measured results below still work."
              : "Starting up. Your case runs as soon as the backend is ready."}
          </p>
        )}
        <p className="hero-note" role="note">
          <Icons.alert size={16} />
          <span><strong>Synthetic data only.</strong> Not clinically validated. Not for medical use.</span>
        </p>
      </div>
      <ol className="guide-steps" aria-label="Guided case steps">
        {GUIDE_STEPS.map((step, i) => {
          const done = guide.done.includes(step.id);
          const current = guide.next?.id === step.id;
          return (
            <li key={step.id} className="guide-step" data-state={done ? "done" : current ? "current" : "todo"} aria-current={current ? "step" : undefined}>
              <span className="guide-index" aria-hidden="true">{done ? <Icons.check size={14} /> : i + 1}</span>
              <div>
                <div className="item-title">{step.title}{done && <span className="sr-only"> (completed)</span>}</div>
                <div className="muted" style={{ fontSize: 13 }}>{step.detail}</div>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

const NEXT_HINT: Record<string, string> = {
  citation: "Open a numbered citation to see its source record.",
  submit: "Choose “Submit for human review” under the answer.",
  approve: "Switch to the reviewer and approve the submitted answer.",
};

/** Slim next-step strip on the other pages while the guided case is in progress. */
export function GuideBar() {
  const guide = useGuide();
  const { route, setPersona, persona } = useApp();
  if (guide.dismissed || guide.complete || !guide.next || guide.next.id === "run" || route.name === "overview") return null;
  const step = GUIDE_STEPS.findIndex((s) => s.id === guide.next!.id) + 1;
  return (
    <div className="guide-bar" role="region" aria-label="Guided case progress">
      <span className="guide-pill">Guided case · step {step} of {GUIDE_STEPS.length}</span>
      <span className="guide-hint">{NEXT_HINT[guide.next.id]}</span>
      <span className="topbar-spacer" />
      {guide.next.id === "approve" && persona !== "reviewer.demo" && (
        <button type="button" className="btn primary sm" onClick={() => { setPersona("reviewer.demo"); navigate("governance"); }}>Switch to reviewer</button>
      )}
      {guide.next.id !== "approve" && route.name !== "agent" && (
        <button type="button" className="btn sm" onClick={() => navigate("agent", guide.runId
          ? { patient: GUIDE_CASE.patientId, open: guide.runId } : { patient: GUIDE_CASE.patientId })}>Open guided run</button>
      )}
      <button type="button" className="btn ghost sm" onClick={() => dismissGuide()} aria-label="Hide guided case">Hide</button>
    </div>
  );
}
