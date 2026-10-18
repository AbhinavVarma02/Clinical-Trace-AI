// Backend availability, free-quota state and the guided-case UI.
import { useEffect, useState } from "react";
import { formatDuration, useAvailability } from "../edge";
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
  const elapsed = useElapsed(a.waitingSince);
  if (a.mode !== "edge" || !a.status) return null;
  if (a.backend === "ready") {
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
  const elapsed = useElapsed(a.waitingSince);
  if (a.mode !== "edge" || !a.status) return null;
  if (a.backend === "waking" || a.backend === "unavailable") {
    return (
      <div className="banner info availability-banner" role="status" aria-live="polite">
        <span className="spinner" aria-hidden="true" />
        <div>
          <strong>{a.backend === "waking" ? "Starting the analysis backend" : "Reconnecting to the analysis backend"} · {formatDuration(elapsed)}</strong>
          <div className="muted" style={{ fontSize: 13 }}>
            The workspace is served instantly from the edge; the Python agent runs on free hosting that sleeps when idle and needs a short
            start-up. Panels fill in automatically as soon as it is ready{elapsed > 120 ? ". This is taking longer than usual; the status check keeps retrying." : "."}
          </div>
        </div>
        <button type="button" className="btn sm" onClick={a.checkNow}>Check now</button>
      </div>
    );
  }
  if (a.backend === "not_configured") {
    return <div className="banner danger" role="alert"><Icons.alert size={18} /><div>This deployment has no analysis backend configured. The interface loads, but agent features are unavailable.</div></div>;
  }
  const q = a.status.inference;
  if (a.status.backend.inference_mode === "llm" && q.quota_state === "exhausted") {
    return (
      <div className="banner warning" role="status">
        <Icons.alert size={18} />
        <div>
          <strong>Today's free live-inference allocation is used up.</strong> Answers still run through the same tools, verification and review,
          using the deterministic planner and composer, and each answer is labelled. Live inference resumes in {formatDuration(q.resets_in_s)} (00:00 UTC).
        </div>
      </div>
    );
  }
  if (a.status.limits.remaining_agent_runs_today <= 0) {
    return <div className="banner warning" role="status"><Icons.alert size={18} /><div><strong>The free daily agent-run limit for this demo is reached.</strong> Browsing, evidence and review remain available; agent runs reset at 00:00 UTC.</div></div>;
  }
  return null;
}

function startGuidedCase(setPatientId: (id: string) => void) {
  setPatientId(GUIDE_CASE.patientId);
  navigate("agent", { patient: GUIDE_CASE.patientId, q: GUIDE_CASE.question, run: "1" });
}

/** Full guided-case card for the overview page. */
export function GuidedCase() {
  const guide = useGuide();
  const { setPatientId, setPersona } = useApp();
  const a = useAvailability();
  const waiting = a.mode === "edge" && (a.backend === "waking" || a.backend === "unavailable");
  const noBackend = a.mode === "edge" && a.backend === "not_configured";
  return (
    <section className="card guide-card" aria-labelledby="guide-title">
      <div className="card-body stack">
        <div className="row-between" style={{ alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
          <div style={{ minWidth: 0, flex: "1 1 320px" }}>
            <span className="eyebrow">Guided case · about 90 seconds</span>
            <h2 id="guide-title" className="guide-title">See one answer go from question to approved, cited artifact</h2>
            <p className="muted" style={{ fontSize: 14, margin: 0 }}>
              A synthetic patient with a 30-day readmission. Every step below calls the real backend: tool execution, claim-level verification
              against versioned sources, and a hash-bound human review.
            </p>
          </div>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {guide.complete ? (
              <button type="button" className="btn" onClick={resetGuide}><Icons.refresh size={15} /> Run it again</button>
            ) : guide.next?.id === "approve" ? (
              <button type="button" className="btn primary" onClick={() => { setPersona("reviewer.demo"); navigate("governance"); }}>
                <Icons.play size={16} /> Approve as reviewer
              </button>
            ) : guide.next?.id === "run" ? (
              <button type="button" className="btn primary" onClick={() => startGuidedCase(setPatientId)} disabled={noBackend}
                aria-describedby={waiting || noBackend ? "guide-wait" : undefined}>
                <Icons.play size={16} /> Start guided case
              </button>
            ) : (
              <button type="button" className="btn primary" onClick={() => navigate("agent", guide.runId
                ? { patient: GUIDE_CASE.patientId, open: guide.runId } : { patient: GUIDE_CASE.patientId })}>
                <Icons.arrow size={16} /> Continue in the agent
              </button>
            )}
          </div>
        </div>
        {(waiting || noBackend) && guide.next?.id === "run" && (
          <p id="guide-wait" className="faint" style={{ fontSize: 12, margin: 0 }}>
            {noBackend
              ? "This deployment has no analysis backend connected yet, so the guided case cannot run here. The measured evidence below is still available."
              : "The backend is starting; the question is sent as soon as it is ready."}
          </p>
        )}
        <ol className="guide-steps">
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
      </div>
    </section>
  );
}

const NEXT_HINT: Record<string, string> = {
  citation: "Open a numbered citation in the answer to inspect the exact source record and version.",
  submit: "Choose “Submit for human review” under the answer.",
  approve: "Switch to the Demo Reviewer persona and approve the submitted artifact.",
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
