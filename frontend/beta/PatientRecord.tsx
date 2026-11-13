import { useState, type ReactNode } from "react";
import { Icons } from "./icons";
import { Badge, Card, EmptyState, Tabs, formatDate, formatUnix, humanize, type Tone } from "./ui";
import type { BetaRecord, BetaView, CoverageState, Section, Serving, SourceRef, Warning } from "./contract";

const SERVING_TONE: Record<Serving, Tone> = { fresh: "success", cached: "info", stale: "warning", rolled_back: "warning", unavailable: "danger" };
const COVERAGE_TONE: Record<CoverageState, Tone> = { returned: "success", returned_none: "neutral", not_imported: "warning" };
const COVERAGE_TEXT: Record<CoverageState, string> = {
  returned: "Returned",
  returned_none: "Returned none",
  not_imported: "Not imported",
};
const IMPORT_TONE: Record<string, Tone> = { imported: "success", returned_none: "neutral", not_imported: "warning", partially_imported: "danger" };

export function DataOrigin({ view }: { view: BetaView }) {
  return (
    <Badge tone="warning" dot title={view.data_origin === "epic_sandbox" ? "Provider-acquired Epic sandbox synthetic patient; not clinical production data." : "Synthetic data from an in-process test transport. No Epic request was made to produce it."}>
      {view.data_origin === "epic_sandbox" ? "Epic sandbox synthetic patient" : view.data_origin === "synthetic_live_fixture" ? "Synthetic LIVE-shaped fixture" : view.data_origin === "fixture" ? "Synthetic fixture" : "Synthetic MOCK data"}
    </Badge>
  );
}

export function WarningList({ warnings }: { warnings: Warning[] }) {
  return (
    <div className="stack" aria-label="Completeness warnings" role="list">
      {warnings.map((warning) => (
        <div key={warning.code} role="listitem" className={`banner ${warning.severity === "warning" ? "warning" : "info"}`}>
          <Icons.alert size={16} />
          <div>{warning.message}</div>
        </div>
      ))}
    </div>
  );
}

export interface RefreshControl {
  /** Undefined when the client cannot refresh (the fixture client): the button stays disabled. */
  start?: () => void;
  running: boolean;
  notice: string | null;
}

export function RefreshStatus({ view, refresh }: { view: BetaView; refresh?: RefreshControl }) {
  const attempt = view.attempt;
  const coverage = view.coverage;
  return (
    <Card
      title="Refresh and coverage"
      subtitle={view.data_origin === "epic_sandbox" ? "What the approved Epic sandbox acquisition returned, and what it did not" : view.mode === "LIVE" ? "What the offline synthetic acquisition returned, and what it did not" : "What the last MOCK acquisition returned, and what it did not"}
      actions={
        refresh?.start ? (
          <button type="button" className="btn sm" disabled={refresh.running} aria-busy={refresh.running} onClick={refresh.start}
            title="Re-read this patient's synthetic MOCK data and publish a new generation">
            <Icons.refresh size={15} /> {refresh.running ? "Refreshing…" : "Refresh MOCK data"}
          </button>
        ) : (
          <button type="button" className="btn sm" disabled title="Refresh is not available with this client">
            <Icons.refresh size={15} /> Refresh unavailable
          </button>
        )
      }
    >
      <div className="stack">
        <div className="row">
          <Badge tone={SERVING_TONE[view.serving]} dot>Serving: {humanize(view.serving)}</Badge>
          {view.freshness && <Badge tone="outline">Evidence freshness: {humanize(view.freshness)}</Badge>}
          {view.completeness && <Badge tone="warning">Completeness: {humanize(view.completeness)}</Badge>}
          {attempt && <Badge tone="neutral">Last attempt: {humanize(attempt.state)} · {attempt.requests ?? 0} requests</Badge>}
          {view.active && <Badge tone="outline">Retrieved {formatUnix(view.active.retrieved_at)}</Badge>}
        </div>
        {refresh?.notice && (
          <div className="banner warning" role="status">
            <Icons.alert size={16} />
            <div>{refresh.notice}</div>
          </div>
        )}
        {view.evidence_error && (
          <div className="banner danger">
            <Icons.alert size={16} />
            <div><strong>Generation failed its integrity check.</strong> Nothing from it is shown ({humanize(view.evidence_error)}).</div>
          </div>
        )}
        {coverage ? (
          <>
            <div className="table-wrap">
              <table className="table">
                <caption className="sr-only">Source coverage</caption>
                <thead>
                  <tr><th scope="col">Source</th><th scope="col">Coverage</th></tr>
                </thead>
                <tbody>
                  {Object.entries(coverage.sources).map(([source, state]) => (
                    <tr key={source}>
                      <td className="mono">{source}</td>
                      <td><Badge tone={COVERAGE_TONE[state]}>{COVERAGE_TEXT[state]}</Badge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="table-wrap">
              <table className="table">
                <caption className="sr-only">Per-operation counts</caption>
                <thead>
                  <tr>
                    <th scope="col">Operation</th><th scope="col" className="num">Matches</th><th scope="col" className="num">Imported</th><th scope="col">Import</th><th scope="col">Execution</th>{view.mode === "LIVE" && <th scope="col">Information codes</th>}
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(coverage.operations).map(([name, counts]) => (
                    <tr key={name}>
                      <td className="mono">{name}</td>
                      <td className="num">{counts.matches ?? "—"}</td>
                      <td className="num">{counts.imported ?? "—"}</td>
                      <td><Badge tone={IMPORT_TONE[counts.import ?? ""] ?? "neutral"}>{humanize(counts.import ?? "unknown")}</Badge></td>
                      <td>{humanize(counts.execution ?? "unknown")}</td>
                      {view.mode === "LIVE" && <td>{counts.informational_codes?.join(", ") || "—"}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <EmptyState title="No coverage yet">No verified generation exists for this patient, so there is nothing to report coverage for.</EmptyState>
        )}
      </div>
    </Card>
  );
}

export function Provenance({ view }: { view: BetaView }) {
  const p = view.provenance;
  if (!p) return null;
  const genuine = view.data_origin === "epic_sandbox";
  return (
    <Card title="Provenance" subtitle="Where this generation came from and how it was built">
      <dl className="kv">
        <dt>Data origin</dt>
        <dd>{view.data_origin === "epic_sandbox" ? "Provider-acquired Epic sandbox synthetic patient" : view.data_origin === "synthetic_live_fixture" ? "Invented LIVE-shaped fixture; no Epic request" : view.data_origin === "fixture" ? "Synthetic fixture (no backend)" : "Synthetic MOCK transport; no Epic request"}</dd>
        <dt>Source / tier</dt>
        <dd>{view.source} / {view.mode}</dd>
        <dt>Generation</dt>
        <dd className="mono">{p.generation_id}</dd>
        {genuine && p.attempt_id && <><dt>Acquisition attempt</dt><dd className="mono">{p.attempt_id}</dd></>}
        {genuine && p.acquisition_started_at !== null && <><dt>Acquisition started (UTC)</dt><dd><ExactTime seconds={p.acquisition_started_at} /></dd></>}
        <dt>Retrieved</dt>
        <dd>{genuine ? <ExactTime seconds={p.retrieved_at} /> : formatUnix(p.retrieved_at)}</dd>
        {genuine && view.active && <><dt>Promoted (UTC)</dt><dd><ExactTime seconds={view.active.promoted_at} /></dd></>}
        <dt>Policy</dt>
        <dd className="mono">{p.policy ?? "—"}</dd>
        <dt>Manifest / mapping</dt>
        <dd className="mono">{p.manifest_version ?? "—"} / {p.mapping_version ?? "—"}</dd>
        <dt>Bundle SHA-256</dt>
        <dd className="mono" style={{ overflowWrap: "anywhere" }}>{p.bundle_sha256 ?? "—"}</dd>
        {p.manifest_sha256 && <><dt>Manifest SHA-256</dt><dd className="mono" style={{ overflowWrap: "anywhere" }}>{p.manifest_sha256}</dd></>}
        {view.publication && <><dt>Publication</dt><dd>{humanize(view.publication)}</dd></>}
        <dt>Resources</dt>
        <dd>{Object.entries(p.resource_counts).map(([type, count]) => `${type} ${count}`).join(" · ") || "—"}</dd>
      </dl>
    </Card>
  );
}

function ExactTime({ seconds }: { seconds: number | null }) {
  const date = seconds === null ? null : new Date(seconds * 1000);
  if (date === null || !Number.isFinite(date.getTime())) return <>—</>;
  const iso = date.toISOString();
  return <time className="mono" dateTime={iso}>{iso}</time>;
}

type Tab = "conditions" | "encounters" | "labs" | "vitals" | "reports" | "notes";

function Source({ source }: { source: SourceRef }) {
  return <span className="mono muted" title="Source resource, version and last update (UTC)" style={{ fontSize: "0.8em" }}>{source.reference}{source.version ? ` v${source.version}` : " · version unavailable"}{source.last_updated && <> · updated <time dateTime={source.last_updated} title={source.last_updated}>{formatDate(source.last_updated, true)} UTC</time></>}</span>;
}

function Truncation({ section }: { section: Section<unknown> }) {
  return section.truncated ? <p className="muted">Showing the first {section.items.length} of {section.total}.</p> : null;
}

function Body<T>({ section, empty, children }: { section: Section<T>; empty: string; children: (item: T, index: number) => ReactNode }) {
  if (section.items.length === 0) return <EmptyState title={empty} />;
  return (
    <>
      <ul className="stack">{section.items.map((item, index) => <li key={index} className="row-between">{children(item, index)}</li>)}</ul>
      <Truncation section={section} />
    </>
  );
}

export function RecordTabs({ record, view }: { record: BetaRecord; view: BetaView }) {
  const [tab, setTab] = useState<Tab>("conditions");
  const noteCoverage = view.coverage?.sources["DocumentReference.metadata"];
  return (
    <Card
      title="Clinical record"
      subtitle="Mapped from the active verified generation; every row names its source resource"
      actions={<DataOrigin view={view} />}
    >
      <div className="muted" style={{ paddingBottom: "var(--space-3)" }}>
        {record.patient.display_name ?? "Unnamed"} · {humanize(record.patient.gender ?? "unknown")}
        {record.patient.birth_date ? ` · born ${formatDate(record.patient.birth_date)}` : ""}
        {record.patient.age_at_last_encounter !== null ? ` · age ${record.patient.age_at_last_encounter} at last encounter` : ""}
        <div><Source source={record.patient.source} /></div>
      </div>
      <Tabs<Tab>
        label="Record sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "conditions", label: "Conditions", count: record.conditions.total },
          { value: "encounters", label: "Encounters", count: record.encounters.total },
          { value: "labs", label: "Labs", count: record.labs.total },
          { value: "vitals", label: "Vital signs", count: record.vitals.total },
          { value: "reports", label: "Reports", count: record.reports.total },
          { value: "notes", label: "Note metadata", count: record.notes.total },
        ]}
      />
      <div role="tabpanel" style={{ paddingTop: "var(--space-3)" }}>
        {tab === "conditions" && (
          <Body section={record.conditions} empty="No conditions returned">
            {(c) => (
              <>
                <span>{c.display ?? "Unnamed condition"} <Source source={c.source} /></span>
                <span className="row muted">
                  {c.category && <Badge tone="outline">{humanize(c.category)}</Badge>}
                  {c.clinical_status && <Badge tone="neutral">{humanize(c.clinical_status)}</Badge>}
                  {formatDate(c.onset)}
                </span>
              </>
            )}
          </Body>
        )}
        {tab === "encounters" && (
          <Body section={record.encounters} empty="No encounters returned">
            {(e) => (
              <>
                <span>{e.reason ?? "No reason recorded"} <Source source={e.source} /></span>
                <span className="row muted">
                  <Badge tone="outline">{e.class_display ?? e.class ?? "Unknown class"}</Badge>
                  {formatDate(e.start)}
                  {e.end && ` – ${formatDate(e.end)}`}
                  {e.status && <Badge tone="neutral">{humanize(e.status)}</Badge>}
                  {e.length_of_stay_days !== null ? ` · ${e.length_of_stay_days} d` : ""}
                </span>
              </>
            )}
          </Body>
        )}
        {(tab === "labs" || tab === "vitals") && (
          <Body section={tab === "labs" ? record.labs : record.vitals} empty={tab === "labs" ? "No laboratory results returned" : "No vital signs returned"}>
            {(o) => (
              <>
                <span>{o.name} <Source source={o.source} /></span>
                <span className="row">
                  <strong>{o.value ?? "—"} {o.unit ?? ""}</strong>
                  {o.unit_missing && <Badge tone="warning">Unit missing</Badge>}
                  <span className="muted">{formatDate(o.effective)}</span>
                </span>
              </>
            )}
          </Body>
        )}
        {tab === "reports" && (
          <Body section={record.reports} empty="No diagnostic reports returned">
            {(r) => (
              <>
                <span>{r.name ?? "Report"}{r.conclusion ? `: ${r.conclusion}` : ""} <Source source={r.source} /></span>
                <span className="row muted"><Badge tone="outline">{r.result_count} linked results</Badge>{formatDate(r.effective)}</span>
              </>
            )}
          </Body>
        )}
        {tab === "notes" && (
          <>
            <Body
              section={record.notes}
              empty="No note metadata returned"
            >
              {(n) => (
                <>
                  <span>{n.title ?? n.type ?? "Document"}{n.source && <> <Source source={n.source} /></>}</span>
                  <span className="row muted"><Badge tone="outline">{n.type ?? "Unknown type"}</Badge>{n.category && <Badge tone="outline">{humanize(n.category)}</Badge>}{n.status && <Badge tone="neutral">{humanize(n.status)}</Badge>}{formatDate(n.date)}</span>
                </>
              )}
            </Body>
            {view.data_origin === "epic_sandbox" ? <p className="muted">Only document metadata is shown. This acquisition does not import note bodies or attachments.</p> : record.notes.items.length === 0 && noteCoverage && (
              <p className="muted">Coverage reports this source as {COVERAGE_TEXT[noteCoverage].toLowerCase()}; note bodies are never imported in this policy.</p>
            )}
          </>
        )}
      </div>
    </Card>
  );
}
