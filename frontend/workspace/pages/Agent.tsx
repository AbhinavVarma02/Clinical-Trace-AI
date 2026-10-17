import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiError, api, type AgentProgress } from "../api";
import { AnswerCard } from "../components/AnswerCard";
import { Icons } from "../components/icons";
import { Card, EmptyState, ErrorState } from "../components/ui";
import { SAFETY_PROBES, SUGGESTED_PROMPTS } from "../demos";
import { useApi, useApp } from "../state";
import type { AgentRun } from "../types";

const STAGES: Record<string, string> = { guard: "Safety rules checked", plan: "Tool plan prepared", act: "Patient-scoped tool completed",
  compose: "Draft prepared", verify: "Claims checked against evidence", review_gate: "Review requirements checked", finalize: "Result finalized" };

function Progress({ startedAt, events }: { startedAt: number; events: AgentProgress[] }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, []);
  const elapsed = (now - startedAt) / 1000;
  return (
    <div className="progress-line" role="status" aria-live="polite">
      <div className="row">
        <span className="spinner" aria-hidden="true" />
        <strong style={{ fontSize: 14 }}>Agent is working · {elapsed.toFixed(1)} s</strong>
      </div>
      <div className="faint" style={{ fontSize: 12 }}>
        Live execution progress. The answer appears after evidence verification completes.
      </div>
      <ol className="muted" style={{ fontSize: 13, margin: 0, paddingLeft: 18 }}>
        {events.map((event, i) => <li key={i}>{STAGES[event.node] ?? event.node}{event.tool ? ` · ${event.tool.replace(/_/g, " ")}` : ""}</li>)}
      </ol>
    </div>
  );
}

export function AgentPage() {
  const { patientId, setPatientId, route, persona } = useApp();
  const patients = useApi(api.patients, "patients");
  const [question, setQuestion] = useState("");
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [sessions, setSessions] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<{ question: string; startedAt: number; events: AgentProgress[]; runId: string | null } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const autoRan = useRef<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const routePatient = route.params.get("patient");

  useEffect(() => {
    if (routePatient && routePatient !== patientId) setPatientId(routePatient);
  }, [routePatient, patientId, setPatientId]);

  useEffect(() => {
    setRuns([]);
    setError(null);
  }, [patientId, persona]);

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed || pending) return;
    setError(null);
    setPending({ question: trimmed, startedAt: Date.now(), events: [], runId: null });
    try {
      const run = await api.runAgentStream({ patient_id: patientId, question: trimmed, session_id: sessions[`${persona}:${patientId}`] ?? null },
        event => setPending(current => current ? { ...current, runId: event.run_id, events: [...current.events, event] } : null));
      setSessions((s) => ({ ...s, [`${persona}:${patientId}`]: run.session_id }));
      setRuns((r) => [...r, run]);
      setQuestion("");
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(0, "unknown", String(err)));
    } finally {
      setPending(null);
    }
  }

  useEffect(() => {
    const q = route.params.get("q");
    const key = `${route.params.toString()}`;
    if (q && route.params.get("run") === "1" && autoRan.current !== key && (!routePatient || routePatient === patientId)) {
      autoRan.current = key;
      void ask(q);
    }
    const open = route.params.get("open");
    if (open && autoRan.current !== key) {
      autoRan.current = key;
      api.run(open).then((run) => setRuns([run])).catch((err: unknown) => setError(err instanceof ApiError ? err : null));
    }
  }, [route, patientId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [runs.length, pending]);

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(question);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void ask(question);
    }
  }

  const prompts = SUGGESTED_PROMPTS[patientId] ?? [];
  const selected = patients.data?.find((p) => p.patient_id === patientId);

  return (
    <div className="split-wide">
      <div className="stack" style={{ gap: "var(--space-4)", minWidth: 0 }}>
        <div className="page-header">
          <div>
            <h1 className="page-title">Case Review Agent</h1>
            <p className="page-subtitle">
              Ask about the selected synthetic record. The agent selects patient-scoped tools, gathers evidence, and only returns claims that pass
              citation, number, and support checks.
            </p>
          </div>
        </div>
        {runs.length === 0 && !pending && !error && (
          <Card>
            <EmptyState title={`Ask about ${selected?.display_name.replace(" (synthetic record)", "") ?? patientId}`}>
              Try a suggested question, or one of the safety probes to see refusals and abstentions.
            </EmptyState>
          </Card>
        )}
        {runs.map((run) => (
          <div key={run.run_id} className="stack">
            <div className="question-bubble">{run.question}</div>
            <AnswerCard run={run} canReview={persona.startsWith("clinician")} />
          </div>
        ))}
        {pending && (
          <div className="stack">
            <div className="question-bubble">{pending.question}</div>
            <Progress startedAt={pending.startedAt} events={pending.events} />
            {pending.runId && <button type="button" className="btn" onClick={() => {
              void api.cancelRun(pending.runId!).catch(err => setError(err instanceof ApiError ? err : new ApiError(0, "cancel_failed", "Cancellation could not be requested.")));
            }}>Cancel execution</button>}
          </div>
        )}
        {error && <Card><ErrorState error={error} onRetry={() => setError(null)} /></Card>}
        <div ref={endRef} />
        <form className="card" onSubmit={onSubmit} style={{ position: "sticky", bottom: 12 }}>
          <div className="card-body stack" style={{ gap: 10 }}>
            <label className="sr-only" htmlFor="question">Question</label>
            <textarea
              id="question"
              className="textarea"
              rows={2}
              maxLength={1000}
              placeholder="Ask about encounters, medications, labs, notes, gaps, or readmission risk…"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={onKeyDown}
              disabled={Boolean(pending)}
            />
            <div className="row-between">
              <span className="faint" style={{ fontSize: 12 }}>Enter to send · Shift+Enter for a new line · {question.length}/1000</span>
              <button type="submit" className="btn primary" disabled={!question.trim() || Boolean(pending)}>
                <Icons.send size={16} /> Ask
              </button>
            </div>
          </div>
        </form>
      </div>

      <div className="stack" style={{ gap: "var(--space-4)" }}>
        <Card title="Patient context">
          <label className="field">
            <span className="field-label">Synthetic patient</span>
            <select className="select" value={patientId} onChange={(e) => setPatientId(e.target.value)} disabled={Boolean(pending)}>
              {(patients.data ?? []).map((p) => (
                <option key={p.patient_id} value={p.patient_id}>
                  {p.display_name.replace(" (synthetic record)", "")} · {p.patient_id}
                </option>
              ))}
            </select>
          </label>
          {patients.error && <ErrorState error={patients.error} onRetry={patients.reload} />}
          {selected && <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>{selected.conditions.join(" · ")}</p>}
          <p className="faint" style={{ fontSize: 12, marginTop: 10 }}>
            The session is bound to this persona and patient; tools cannot be redirected to another record.
          </p>
        </Card>
        <Card title="Suggested questions">
          <div className="prompt-chips">
            {prompts.map((prompt) => (
              <button key={prompt} type="button" className="chip" onClick={() => void ask(prompt)} disabled={Boolean(pending)}>
                {prompt}
              </button>
            ))}
          </div>
        </Card>
        <Card title="Safety probes" subtitle="These should be refused or abstained on">
          <div className="prompt-chips">
            {SAFETY_PROBES.map((prompt) => (
              <button key={prompt} type="button" className="chip" onClick={() => void ask(prompt)} disabled={Boolean(pending)}>
                {prompt}
              </button>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
