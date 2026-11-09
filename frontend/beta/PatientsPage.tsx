import { useState } from "react";
import { Icons } from "./icons";
import { Badge, Card, EmptyState, Skeleton, Tabs, formatDate, humanize, type Tone } from "./ui";
import type { CoverageState, FixtureRecord, FixtureStatus, Serving } from "./fixtures";
import { useBeta, useLoad } from "./state";

const SERVING_TONE: Record<Serving, Tone> = { fresh: "success", cached: "info", stale: "warning", rolled_back: "warning", unavailable: "danger" };
const COVERAGE_TONE: Record<CoverageState, Tone> = { returned: "success", returned_none: "neutral", not_imported: "warning" };
const COVERAGE_TEXT: Record<CoverageState, string> = {
  returned: "Returned",
  returned_none: "Returned none",
  not_imported: "Not imported",
};

function PatientPicker({ patients }: { patients: { alias: string; label: string }[] }) {
  const { alias, selectAlias } = useBeta();
  return (
    <Card title="Approved MOCK patients" subtitle="Fixed local policy; no search and no identifiers" flush>
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
              <div className="item-meta">Synthetic fixture</div>
            </div>
          </button>
        ))}
      </div>
    </Card>
  );
}

export function RefreshStatus({ status }: { status: FixtureStatus }) {
  const attempt = status.attempt;
  return (
    <Card
      title="Refresh and coverage"
      subtitle="What the last MOCK acquisition returned, and what it did not"
      actions={
        <button type="button" className="btn sm" disabled title="Refresh is not wired in this MOCK slice">
          <Icons.refresh size={15} /> Refresh unavailable
        </button>
      }
    >
      <div className="stack">
        <div className="row">
          <Badge tone={SERVING_TONE[status.serving]} dot>Serving: {humanize(status.serving)}</Badge>
          {attempt && <Badge tone="neutral">Last attempt: {humanize(attempt.state)} · {attempt.requests} requests</Badge>}
          {status.active && <Badge tone="outline">Retrieved {formatDate(status.active.retrieved_at, true)}</Badge>}
        </div>
        <div className="table-wrap">
          <table className="table">
            <caption className="sr-only">Source coverage</caption>
            <thead>
              <tr><th scope="col">Source</th><th scope="col">Coverage</th></tr>
            </thead>
            <tbody>
              {Object.entries(status.coverage).map(([source, state]) => (
                <tr key={source}>
                  <td className="mono">{source}</td>
                  <td><Badge tone={COVERAGE_TONE[state]}>{COVERAGE_TEXT[state]}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {attempt && (
          <div className="table-wrap">
            <table className="table">
              <caption className="sr-only">Per-operation counts</caption>
              <thead>
                <tr>
                  <th scope="col">Operation</th><th scope="col" className="num">Matches</th><th scope="col" className="num">Imported</th><th scope="col">Execution</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(attempt.operations).map(([name, counts]) => (
                  <tr key={name}>
                    <td className="mono">{name}</td>
                    <td className="num">{counts.matches}</td>
                    <td className="num">{counts.imported}</td>
                    <td>{humanize(counts.execution)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="banner warning">
          <Icons.alert size={16} />
          <div>
            <strong>Note bodies are not imported.</strong> Only note metadata is shown (reason: {humanize(attempt?.note_body_reason ?? "outside_initial_policy")}). Medications are not imported, so absence here is not evidence of absence.
          </div>
        </div>
      </div>
    </Card>
  );
}

export function EvidencePlaceholder({ generationId }: { generationId?: string }) {
  return (
    <Card title="Evidence" subtitle="Placeholder: no answer has been run in this slice">
      <div className="stack">
        <div className="banner info">
          <Icons.evidence size={16} />
          <div>
            Citations and evidence pins appear here after a verified answer from a pinned generation. Nothing on this page is cited evidence, and no clinical answer is produced here.
          </div>
        </div>
        <dl className="kv">
          <dt>Pinned generation</dt>
          <dd className="mono">{generationId ?? "—"} (fixture)</dd>
          <dt>Citations</dt>
          <dd>None yet</dd>
          <dt>Independent review</dt>
          <dd>Pending: not requested in this slice</dd>
        </dl>
      </div>
    </Card>
  );
}

type Tab = "conditions" | "encounters" | "labs" | "notes";

function RecordTabs({ record }: { record: FixtureRecord }) {
  const [tab, setTab] = useState<Tab>("conditions");
  return (
    <Card
      title="Clinical record"
      subtitle="Synthetic fixture content; the Beta API has no record route yet"
      actions={<Badge tone="warning" dot>Synthetic record</Badge>}
    >
      <Tabs<Tab>
        label="Record sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "conditions", label: "Conditions", count: record.conditions.length },
          { value: "encounters", label: "Encounters", count: record.encounters.length },
          { value: "labs", label: "Labs", count: record.labs.length },
          { value: "notes", label: "Note metadata", count: record.notes.length },
        ]}
      />
      <div role="tabpanel" style={{ paddingTop: "var(--space-3)" }}>
        {tab === "conditions" &&
          (record.conditions.length === 0 ? <EmptyState title="No conditions returned" /> : (
            <ul className="stack">
              {record.conditions.map((c) => (
                <li key={c.display} className="row-between">
                  <span>{c.display}</span>
                  <span className="row muted"><Badge tone="outline">{humanize(c.category)}</Badge>{formatDate(c.recorded)}</span>
                </li>
              ))}
            </ul>
          ))}
        {tab === "encounters" &&
          (record.encounters.length === 0 ? <EmptyState title="No encounters returned" /> : (
            <ul className="stack">
              {record.encounters.map((e) => (
                <li key={e.id} className="row-between">
                  <span>{e.reason}</span>
                  <span className="row muted"><Badge tone="outline">{e.class}</Badge>{formatDate(e.start)}</span>
                </li>
              ))}
            </ul>
          ))}
        {tab === "labs" &&
          (record.labs.length === 0 ? <EmptyState title="No laboratory results returned" /> : (
            <ul className="stack">
              {record.labs.map((l) => (
                <li key={`${l.name}:${l.date}`} className="row-between">
                  <span>{l.name}</span>
                  <span className="row"><strong>{l.value} {l.unit}</strong><span className="muted">{formatDate(l.date)}</span></span>
                </li>
              ))}
            </ul>
          ))}
        {tab === "notes" &&
          (record.notes.length === 0 ? (
            <EmptyState title="No note metadata returned">Coverage reports this source as returned none; note bodies are never imported in this policy.</EmptyState>
          ) : (
            <ul className="stack">
              {record.notes.map((n) => (
                <li key={n.title} className="row-between">
                  <span>{n.title}</span>
                  <span className="row muted"><Badge tone="outline">{n.type}</Badge>{formatDate(n.date)}</span>
                </li>
              ))}
            </ul>
          ))}
      </div>
    </Card>
  );
}

function PatientDetail({ alias }: { alias: string }) {
  const { client } = useBeta();
  const record = useLoad(() => client.getRecord(alias), alias);
  const status = useLoad(() => client.getStatus(alias), alias);
  if (record.error || status.error) {
    return <EmptyState title="Patient unavailable">This patient is not available for the current session.</EmptyState>;
  }
  if (!record.data || !status.data) return <Card><Skeleton lines={5} /></Card>;
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <RefreshStatus status={status.data} />
      <RecordTabs record={record.data} />
      <EvidencePlaceholder generationId={status.data.active?.generation_id} />
    </div>
  );
}

export function PatientsPage() {
  const { client, alias } = useBeta();
  const patients = useLoad(() => client.listPatients(), "patients");
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">Patients</h1>
          <p className="muted">Epic Beta · MOCK fixtures only</p>
        </div>
      </div>
      <div className="grid cols-2" style={{ gridTemplateColumns: "minmax(220px, 1fr) minmax(0, 3fr)" }}>
        {patients.data ? <PatientPicker patients={patients.data} /> : <Card><Skeleton /></Card>}
        {alias ? <PatientDetail key={alias} alias={alias} /> : <EmptyState title="Select a patient">Choose an approved MOCK patient to see the record, refresh status and coverage.</EmptyState>}
      </div>
    </div>
  );
}
