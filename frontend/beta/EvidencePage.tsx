// The last returned answer only. Every admission rechecks the current session, approved alias and active generation.
import { useEffect, useState } from "react";
import { BetaApiError } from "./client";
import { AnswerCard, EvidenceCard } from "./AgentPage";
import type { BetaView } from "./contract";
import { DataOrigin, Provenance, WarningList } from "./PatientRecord";
import { useBeta } from "./state";
import { Card, EmptyState, SignIn, Skeleton, humanize } from "./ui";

export function EvidencePage() {
  const { client, alias, latestRun, clearRun, pollMs } = useBeta();
  const [checked, setChecked] = useState<{ run: typeof latestRun; view: BetaView } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setChecked(null);
    if (!latestRun || !client.getSession || alias !== latestRun.alias) return;
    setError(null);
    let active = true;
    let sequence = 0;
    let poll: ReturnType<typeof setTimeout>;
    let expiry: ReturnType<typeof setTimeout>;
    const check = async () => {
      const request = ++sequence;
      clearTimeout(poll);
      try {
        const before = await client.getSession!();
        if (before.binding !== latestRun.sessionBinding) throw new Error("session_changed");
        const patients = await client.listPatients();
        if (!patients.some((patient) => patient.alias === alias)) throw new Error("patient_unavailable");
        const view = await client.getView(alias);
        const after = await client.getSession!();
        if (after.binding !== latestRun.sessionBinding || before.expiresAt <= Date.now() || after.expiresAt <= Date.now()) throw new Error("session_changed");
        if (view.mode !== "MOCK" || view.alias !== alias || view.active?.generation_id !== latestRun.generationId || view.provenance?.generation_id !== latestRun.generationId || !view.record || view.evidence_error || view.serving === "unavailable") throw new Error("generation_unavailable");
        if (!active || request !== sequence) return;
        setChecked({ run: latestRun, view });
        clearTimeout(expiry);
        expiry = setTimeout(() => {
          if (active) { setChecked(null); setError("owner_required"); clearRun(); }
        }, Math.min(before.expiresAt, after.expiresAt) - Date.now());
        // Non-renewing reads only. No refresh, question, session heartbeat or background owner action.
        poll = setTimeout(() => void check(), Math.max(100, pollMs * 5));
      } catch (failure) {
        if (!active || request !== sequence) return;
        setChecked(null);
        setError(failure instanceof BetaApiError ? failure.code : failure instanceof Error ? failure.message : "unavailable");
        clearRun();
      }
    };
    const foreground = () => {
      setChecked(null);
      if (document.visibilityState !== "hidden") void check();
      else { sequence += 1; clearTimeout(poll); }
    };
    void check();
    window.addEventListener("focus", foreground);
    document.addEventListener("visibilitychange", foreground);
    return () => {
      active = false;
      clearTimeout(poll);
      clearTimeout(expiry);
      window.removeEventListener("focus", foreground);
      document.removeEventListener("visibilitychange", foreground);
    };
    // clearRun changes identity with context renders; only the retained run and selected alias identify admission.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, alias, latestRun, pollMs]);

  const validated = checked?.run === latestRun && latestRun?.alias === alias ? checked : null;
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <div className="page-header"><div><h1 className="page-title">Evidence Explorer</h1><p className="muted">Latest returned MOCK answer only · no run history or retrieval scores</p></div></div>
      {error === "owner_required" || error === "session_changed" ? <SignIn expired /> : error ? <EmptyState title="Evidence withheld">The session, patient or verified active generation could not be confirmed. Ask again on the Case Review Agent page when a usable generation is available.</EmptyState> : !latestRun || !client.getSession || latestRun.alias !== alias ? <EmptyState title="No retained answer">Ask a question on the Case Review Agent page, then open Evidence Explorer. Questions are never rerun automatically.</EmptyState> : !validated ? <Card title="Checking evidence access"><Skeleton /></Card> : <>
        <Card title="Run provenance" actions={<DataOrigin view={validated.view} />}><dl className="kv"><dt>Patient</dt><dd>{validated.view.label}</dd><dt>Run</dt><dd className="mono">{latestRun.answer.run_id}</dd><dt>Generation</dt><dd className="mono">{latestRun.generationId}</dd><dt>Serving at answer time</dt><dd>{humanize(latestRun.answer.serving)}</dd></dl></Card>
        <WarningList warnings={validated.view.warnings} />
        <AnswerCard answer={latestRun.answer} />
        <EvidenceCard answer={latestRun.answer} />
        {latestRun.answer.evidence.length === 0 && <EmptyState title="No evidence returned">This run returned no evidence; no citations have been created here.</EmptyState>}
        <Provenance view={validated.view} />
      </>}
    </div>
  );
}
