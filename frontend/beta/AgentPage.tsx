// Case Review Agent: ask one question of one patient's active MOCK generation and show exactly what the server
// verified. The page adds nothing of its own: no claim, citation, risk or recommendation is created here.
import { useEffect, useRef, useState } from "react";
import { Icons } from "./icons";
import { BetaApiError, MAX_QUESTION } from "./client";
import type { Answer, BetaView, Claim } from "./contract";
import { DataOrigin } from "./PatientRecord";
import { PatientPicker } from "./PatientsPage";
import { useBeta, useLoad } from "./state";
import { Badge, Card, EmptyState, SignIn, Skeleton, formatDate, formatUnix, humanize } from "./ui";

const SUGGESTIONS = ["What is the latest creatinine?", "Summarize this patient's record.", "Which conditions are recorded?"];

function askNotice(error: unknown): string {
  if (error instanceof BetaApiError) {
    switch (error.code) {
      case "patient_scope":
        return "That question refers to a different patient. Ask about the selected patient only.";
      case "invalid_question":
        return `Enter a question of 1 to ${MAX_QUESTION} characters.`;
      case "generation_expired":
        return "This generation is too old to use. Refresh the patient on the Patients page first.";
      case "generation_integrity":
      case "run_interrupted":
        return "This generation failed its integrity check, so no answer was produced.";
      case "clinical_run_failed":
        return "The question could not be answered. Nothing was recorded as an answer.";
      case "owner_required":
        return "Your session ended. Sign in again to ask a question.";
      default:
        return `The question could not be asked (${error.code.replace(/_/g, " ")}).`;
    }
  }
  return "The question could not be asked.";
}

function Citations({ claim, evidence }: { claim: Claim; evidence: Answer["evidence"] }) {
  const focus = (index: number) => {
    const target = document.getElementById(`evidence-${index}`);
    target?.scrollIntoView({ block: "nearest" });
    target?.focus();
  };
  return (
    <span className="row" aria-label="Citations">
      {claim.citations.map((id) => {
        const index = evidence.findIndex((item) => item.evidence_id === id);
        return index < 0 ? (
          <Badge key={id} tone="danger" title="This citation does not match any returned evidence">Unresolved</Badge>
        ) : (
          <button key={id} type="button" className="btn sm" onClick={() => focus(index)} aria-label={`Evidence ${index + 1}`}>
            [{index + 1}]
          </button>
        );
      })}
    </span>
  );
}

export function AnswerCard({ answer }: { answer: Answer }) {
  const refused = answer.outcome === "refused";
  const verified = answer.verification?.status === "verified";
  const content = answer.answer;
  return (
    <Card
      title="Answer"
      subtitle={answer.question}
      actions={
        <>
          <Badge tone={answer.outcome === "answered" ? "success" : refused ? "danger" : "warning"} dot>{humanize(answer.outcome)}</Badge>
          {answer.verification && (
            <Badge tone={verified ? "success" : "danger"}>
              {verified
                ? `Verified ${answer.verification.claims_supported} of ${answer.verification.claims_total} claims`
                : `Verification ${answer.verification.status}`}
            </Badge>
          )}
        </>
      }
    >
      <div className="stack">
        {answer.refusal && (
          <div className="banner danger" role="status">
            <Icons.alert size={16} />
            <div><strong>Not answered.</strong> {answer.refusal.message}</div>
          </div>
        )}
        {content?.abstain_reason && (
          <div className="banner warning">
            <Icons.alert size={16} />
            <div>No answer was given: {humanize(content.abstain_reason)}.</div>
          </div>
        )}
        {content && content.claims.length > 0 && (
          <>
            {content.summary && <p><strong>{content.summary}</strong></p>}
            <ol className="stack" aria-label="Claims">
              {content.claims.map((claim, index) => (
                <li key={index} className="row-between">
                  <span>
                    {claim.text}
                    {claim.computations?.map((c, i) => (
                      <span key={i} className="muted mono" style={{ display: "block", fontSize: "0.8em" }}>
                        computed: {c.op}({c.args.join(", ")}) = {c.result}
                      </span>
                    ))}
                  </span>
                  <Citations claim={claim} evidence={answer.evidence} />
                </li>
              ))}
            </ol>
          </>
        )}
        {content && content.uncertainty.length > 0 && (
          <div className="banner warning" aria-label="Uncertainty">
            <Icons.alert size={16} />
            <div>
              <strong>What this answer cannot establish</strong>
              <ul>{content.uncertainty.map((line) => <li key={line}>{line}</li>)}</ul>
            </div>
          </div>
        )}
        {answer.review.required && (
          <div className="banner info">
            <Icons.evidence size={16} />
            <div>
              <strong>Independent review required and pending.</strong> This owner session cannot approve answers
              ({answer.review.reasons.map((r) => humanize(r)).join(", ") || "review required"}). Nothing here is clinically approved.
            </div>
          </div>
        )}
        <div className="muted">{answer.inference.label} · {answer.disclaimer}</div>
      </div>
    </Card>
  );
}

export function EvidenceCard({ answer }: { answer: Answer }) {
  if (answer.evidence.length === 0) return null;
  return (
    <Card title="Evidence" subtitle="Records retrieved for this question; each row names its source resource and version">
      <ul className="stack" aria-label="Evidence">
        {answer.evidence.map((item, index) => (
          <li key={item.evidence_id} id={`evidence-${index}`} tabIndex={-1} className="stack" style={{ gap: "var(--space-1)" }}>
            <div className="row-between">
              <strong>[{index + 1}] {item.title ?? "Record"}</strong>
              <span className="row">
                <Badge tone={item.cited ? "success" : "neutral"}>{item.cited ? "Cited" : "Retrieved, not cited"}</Badge>
                {item.date && <span className="muted">{formatDate(item.date)}</span>}
              </span>
            </div>
            <div>{item.text}</div>
            <div className="mono muted" style={{ fontSize: "0.8em" }}>{item.source_ref} v{item.source_version}</div>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function Ask({ alias }: { alias: string }) {
  const { client, clearRun, retainRun } = useBeta();
  const view = useLoad<BetaView>(() => client.getView(alias), alias);
  const [question, setQuestion] = useState("");
  const [running, setRunning] = useState(false);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [needsAck, setNeedsAck] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  if (view.error === "owner_required") return <SignIn expired />;
  if (view.error) return <EmptyState title="Patient unavailable">This patient is not available for the current session.</EmptyState>;
  if (!view.data) return <Card><Skeleton lines={4} /></Card>;
  const data = view.data;
  const generation = data.active?.generation_id;
  if (data.mode === "LIVE") return <EmptyState title="Questions are not available">This synthetic LIVE-shaped record is a read-only preview. Clinical AI is disabled.</EmptyState>;
  if (!client.ask) {
    return <EmptyState title="Questions are not available">This client cannot ask questions; it only shows fixtures.</EmptyState>;
  }
  if (!generation || data.serving === "unavailable") {
    return (
      <EmptyState title="No usable generation">
        {data.evidence_error ? "This generation failed its integrity check." : "This patient has no verified generation yet."} Refresh it on the Patients page first.
      </EmptyState>
    );
  }

  const run = async (acknowledge: boolean) => {
    const text = question.trim();
    if (!text) return;
    setRunning(true);
    setNotice(null);
    setNeedsAck(false);
    setAnswer(null); // a new question never leaves the previous answer standing under it
    const revision = clearRun();
    try {
      const session = await client.getSession?.();
      const result = await client.ask!(alias, generation, text, acknowledge);
      if (mounted.current) {
        setAnswer(result);
        if (session) retainRun({ alias, generationId: generation, sessionBinding: session.binding, answer: result }, revision);
      }
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof BetaApiError && error.code === "cache_acknowledgement_required") setNeedsAck(true);
      else setNotice(askNotice(error));
    } finally {
      if (mounted.current) setRunning(false);
    }
  };

  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <Card
        title="Ask about this patient"
        subtitle={`Pinned to generation ${generation.slice(0, 12)}…, retrieved ${formatUnix(data.active?.retrieved_at)}`}
        actions={<DataOrigin view={data} />}
      >
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            void run(false);
          }}
        >
          <label htmlFor="question" className="muted">Question (the record only; no treatment advice, one patient)</label>
          <textarea
            id="question"
            value={question}
            maxLength={MAX_QUESTION}
            rows={3}
            onChange={(event) => setQuestion(event.target.value)}
            style={{ width: "100%", font: "inherit", padding: "var(--space-3)", borderRadius: "var(--radius-md)", border: "1px solid var(--border-strong)", background: "var(--surface)", color: "var(--text)" }}
          />
          <div className="row">
            {SUGGESTIONS.map((suggestion) => (
              <button key={suggestion} type="button" className="btn sm ghost" onClick={() => setQuestion(suggestion)}>{suggestion}</button>
            ))}
          </div>
          <div className="row">
            <button type="submit" className="btn primary" disabled={running || !question.trim()} aria-busy={running}>
              {running ? "Asking…" : "Ask"}
            </button>
            <span className="muted">{question.length}/{MAX_QUESTION}</span>
            {data.serving !== "fresh" && <Badge tone="warning" dot>Data is {humanize(data.serving).toLowerCase()}</Badge>}
          </div>
          {notice && <div className="banner warning" role="status"><Icons.alert size={16} /><div>{notice}</div></div>}
          {needsAck && (
            <div className="banner warning" role="alert">
              <Icons.alert size={16} />
              <div>
                <p>This generation is {humanize(data.serving).toLowerCase()}, not the result of the latest refresh. An answer would rest on that older data.</p>
                <button type="button" className="btn sm" onClick={() => void run(true)}>Ask on {humanize(data.serving).toLowerCase()} data</button>
              </div>
            </div>
          )}
        </form>
      </Card>
      {answer && <AnswerCard answer={answer} />}
      {answer && <EvidenceCard answer={answer} />}
      {answer && client.getSession && <a className="btn" href="#/evidence">Open Evidence Explorer</a>}
    </div>
  );
}

export function AgentPage() {
  const { client, alias } = useBeta();
  const patients = useLoad(() => client.listPatients(), "agent-patients");
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">Case Review Agent</h1>
          <p className="muted">Grounded questions and answers · synthetic MOCK data only · deterministic, no language model</p>
        </div>
      </div>
      <div className="grid patients-grid">
        {patients.error === "owner_required" ? (
          <SignIn />
        ) : patients.data ? (
          <PatientPicker patients={patients.data} />
        ) : (
          <Card><Skeleton /></Card>
        )}
        {alias ? <Ask key={alias} alias={alias} /> : <EmptyState title="Select a patient">Choose an approved MOCK patient, then ask a question about the record.</EmptyState>}
      </div>
    </div>
  );
}
