import { useEffect, useRef, useState } from "react";
import { Icons } from "./icons";
import { Card, EmptyState, Skeleton, SignIn } from "./ui";
import type { PatientChoice } from "./contract";
import { BetaApiError } from "./client";
import { useBeta, useLoad, usePatientView } from "./state";
import { DataOrigin, Provenance, RecordTabs, RefreshStatus, WarningList } from "./PatientRecord";

export function PatientPicker({ patients }: { patients: PatientChoice[] }) {
  const { alias, selectAlias } = useBeta();
  return (
    <Card title={patients.some((p) => p.data_origin === "synthetic_live_fixture") ? "Approved synthetic patients" : "Approved MOCK patients"} subtitle="Fixed local policy; no search and no identifiers" flush>
      <div className="list">
        {patients.map((patient) => (
          <button
            key={patient.alias}
            type="button"
            className="list-item"
            aria-current={patient.alias === alias}
            onClick={() => selectAlias(patient.alias)}
          >
            <span className="avatar" aria-hidden="true">{patient.label.slice(-1)}</span>
            <div style={{ minWidth: 0 }}>
              <div className="item-title">{patient.label}</div>
              <div className="item-meta">{patient.data_origin === "synthetic_live_fixture" ? "Synthetic LIVE-shaped fixture · read only" : "Synthetic MOCK patient"}</div>
            </div>
          </button>
        ))}
      </div>
    </Card>
  );
}

export function EvidencePlaceholder({ generationId }: { generationId?: string }) {
  return (
    <Card title="Evidence" subtitle="Evidence is available only for a returned answer">
      <div className="stack">
        <div className="banner info">
          <Icons.evidence size={16} />
          <div>
            Ask a question on the Case Review Agent page, then open Evidence Explorer to inspect its returned citations. This record page does not create an answer or cited evidence.
          </div>
        </div>
        <dl className="kv">
          <dt>Pinned generation</dt>
          <dd className="mono">{generationId ?? "—"}</dd>
          <dt>Answer evidence</dt>
          <dd><a href="#/evidence">Open Evidence Explorer</a></dd>
          <dt>Independent review</dt>
          <dd>No review decision is available on this page</dd>
        </dl>
      </div>
    </Card>
  );
}

const SETTLED = new Set(["succeeded", "failed", "interrupted"]);
const MAX_POLLS = 120;

function refreshNotice(error: unknown): string {
  if (error instanceof BetaApiError) {
    if (error.code === "refresh_busy") return "A refresh is already running. Wait for it to finish.";
    if (error.code === "quota_exhausted") {
      return error.retryAfter
        ? `Refresh limit reached. Try again in ${error.retryAfter >= 120 ? `${Math.ceil(error.retryAfter / 60)} minutes` : `${error.retryAfter} seconds`}.`
        : "Refresh limit reached. Try again later.";
    }
    if (error.code === "owner_required") return "Your session ended. Sign in again to refresh.";
    return `The refresh could not be started (${error.code.replace(/_/g, " ")}).`;
  }
  return "The refresh could not be started.";
}

function RevokeControl({ alias, onRevoked, onAccessLost }: { alias: string; onRevoked: () => void; onAccessLost: (error: string) => void }) {
  const { client } = useBeta();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  if (!client.revoke) return null;
  const revoke = async () => {
    setBusy(true);
    setNotice(null);
    try {
      await client.revoke!(alias);
      onRevoked();
    } catch (error) {
      if (error instanceof BetaApiError && [401, 403, 404].includes(error.status)) onAccessLost(error.code);
      setNotice(error instanceof BetaApiError && error.code === "owner_required" ? "Your session ended. Sign in again." : "The patient could not be revoked.");
      setBusy(false);
      setConfirming(false);
    }
  };
  return (
    <Card title="Owner controls" subtitle="Authorization for this patient">
      <div className="stack">
        <p className="muted">
          Revoking removes this patient from the approved list and makes its record, refreshes and questions unavailable in this owner store.
          It cannot be undone from the browser.
        </p>
        {notice && <div className="banner warning" role="status"><Icons.alert size={16} /><div>{notice}</div></div>}
        {confirming ? (
          <div className="row">
            <button type="button" className="btn danger" disabled={busy} onClick={() => void revoke()}>{busy ? "Revoking…" : "Confirm: revoke this patient"}</button>
            <button type="button" className="btn ghost" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        ) : (
          <div><button type="button" className="btn danger" onClick={() => setConfirming(true)}>Revoke access…</button></div>
        )}
      </div>
    </Card>
  );
}

function PatientDetail({ alias, onRevoked }: { alias: string; onRevoked: () => void }) {
  const { client, pollMs } = useBeta();
  const view = usePatientView(alias);
  const [running, setRunning] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const start = client.refresh && view.data?.mode !== "LIVE"
    ? async () => {
        setRunning(true);
        setNotice(null);
        try {
          await client.refresh!(alias);
          for (let polls = 0; polls < MAX_POLLS && mounted.current; polls += 1) {
            await new Promise((resolve) => setTimeout(resolve, pollMs));
            const next = await view.reload();
            if (!mounted.current) return;
            if (!next) break;
            if (next.attempt && SETTLED.has(next.attempt.state)) break;
          }
        } catch (error) {
          if (mounted.current && error instanceof BetaApiError && [401, 403, 404].includes(error.status)) view.invalidate(error.code);
          if (mounted.current) setNotice(refreshNotice(error));
        } finally {
          if (mounted.current) setRunning(false);
        }
      }
    : undefined;

  if (view.error) {
    if (view.error === "owner_required") return <SignIn expired />;
    return <EmptyState title="Patient unavailable">This patient is not available for the current session.</EmptyState>;
  }
  const data = view.data;
  if (!data) return <Card><Skeleton lines={5} /></Card>;
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <WarningList warnings={data.warnings} />
      {data.record ? (
        <RecordTabs record={data.record} view={data} />
      ) : (
        <Card title="Clinical record" actions={<DataOrigin view={data} />}>
          <EmptyState title="No verified record yet">No active generation exists for this patient, so no clinical content is shown.</EmptyState>
        </Card>
      )}
      <RefreshStatus view={data} refresh={{ start, running, notice }} />
      <Provenance view={data} />
      {data.mode === "LIVE" ? <Card title="Read-only synthetic preview"><p>Clinical AI and answer evidence are unavailable for this record. No claims or citations have been created.</p></Card> : <EvidencePlaceholder generationId={data.active?.generation_id} />}
      <RevokeControl alias={alias} onRevoked={onRevoked} onAccessLost={view.invalidate} />
    </div>
  );
}

export function PatientsPage() {
  const { client, alias, selectAlias } = useBeta();
  const [version, setVersion] = useState(0); // bumped after a revoke so the approved list is read again
  const patients = useLoad(() => client.listPatients(), `patients:${version}`);
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">Patients</h1>
          <p className="muted">Epic Beta · synthetic data only</p>
        </div>
      </div>
      <div className="grid patients-grid">
        {patients.error === "owner_required" ? (
          <SignIn />
        ) : patients.error ? (
          <EmptyState title="Patients unavailable">The approved patient list could not be read.</EmptyState>
        ) : patients.data ? (
          <PatientPicker patients={patients.data} />
        ) : (
          <Card><Skeleton /></Card>
        )}
        {alias ? <PatientDetail key={alias} alias={alias} onRevoked={() => { selectAlias(null); setVersion((v) => v + 1); }} /> : <EmptyState title="Select a patient">Choose an approved synthetic patient to see the record, refresh status and coverage.</EmptyState>}
      </div>
    </div>
  );
}
