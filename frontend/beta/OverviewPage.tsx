// Separate passive reads of approved MOCK aliases; no clinical aggregation or invented readiness scores.
import { DataOrigin, Provenance, WarningList } from "./PatientRecord";
import { useBeta, useLoad, usePatientView } from "./state";
import { Badge, Card, EmptyState, SignIn, Skeleton, formatUnix, humanize } from "./ui";

function PatientSummary({ alias, label }: { alias: string; label: string }) {
  const view = usePatientView(alias);
  if (view.error === "owner_required") return <SignIn expired />;
  if (view.error) return <Card title={label}><EmptyState title="Patient unavailable">No record or coverage can be shown.</EmptyState></Card>;
  if (!view.data) return <Card title={label}><Skeleton /></Card>;
  const data = view.data;
  return (
    <div className="stack">
      <Card title={label} actions={<DataOrigin view={data} />}>
        <div className="stack">
          <Badge tone={data.serving === "fresh" ? "success" : "warning"} dot>Serving: {humanize(data.serving)}</Badge>
          {data.freshness && <Badge tone="outline">Evidence freshness: {humanize(data.freshness)}</Badge>}
          {data.completeness && <Badge tone="warning">Completeness: {humanize(data.completeness)}</Badge>}
          <dl className="kv">
            <dt>Last attempt</dt><dd>{data.attempt ? humanize(data.attempt.state) : "No attempt yet"}</dd>
            <dt>Requests reported</dt><dd>{data.attempt?.requests ?? "—"}</dd>
            <dt>Retrieved</dt><dd>{formatUnix(data.active?.retrieved_at)}</dd>
          </dl>
          {data.evidence_error && <div className="banner danger">Generation failed its integrity check; coverage and provenance are unavailable.</div>}
          {data.coverage ? (
            <div className="table-wrap"><table className="table">
              <caption className="sr-only">{label} source coverage</caption>
              <thead><tr><th scope="col">Source</th><th scope="col">Coverage</th></tr></thead>
              <tbody>{Object.entries(data.coverage.sources).map(([source, status]) => <tr key={source}><td className="mono">{source}</td><td>{humanize(status)}</td></tr>)}</tbody>
            </table></div>
          ) : <EmptyState title="No verified coverage">No coverage is available for this patient.</EmptyState>}
          <WarningList warnings={data.warnings} />
        </div>
      </Card>
      <Provenance view={data} />
    </div>
  );
}

export function OverviewPage() {
  const { client } = useBeta();
  const patients = useLoad(() => client.listPatients(), "overview-patients");
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <div className="page-header"><div><h1 className="page-title">Overview</h1><p className="muted">Approved synthetic patients · serving state, coverage and warnings from verified generations</p></div></div>
      {patients.error === "owner_required" ? <SignIn /> : patients.error ? <EmptyState title="Patients unavailable">The approved patient list could not be read.</EmptyState> : !patients.data ? <Card><Skeleton /></Card> : patients.data.length === 0 ? <EmptyState title="No approved patients">No MOCK patient is available for this session.</EmptyState> : patients.data.map((patient) => <PatientSummary key={patient.alias} {...patient} />)}
    </div>
  );
}
