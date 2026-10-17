import { useMemo, useState } from "react";
import { api } from "../api";
import { LabTrendChart } from "../components/charts";
import { Icons } from "../components/icons";
import { Badge, Card, Drawer, EmptyState, ErrorState, Skeleton, Tabs, formatDate, humanize } from "../components/ui";
import { navigate, useApi, useApp } from "../state";
import type { DocumentMeta, PatientDetail } from "../types";

type Tab = "timeline" | "conditions" | "medications" | "labs" | "notes" | "quality";

export function PatientList({ compact = false }: { compact?: boolean }) {
  const { patientId, setPatientId } = useApp();
  const patients = useApi(api.patients, "patients");
  const [query, setQuery] = useState("");
  const filtered = (patients.data ?? []).filter((p) =>
    `${p.display_name} ${p.patient_id} ${p.conditions.join(" ")}`.toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <Card title="Synthetic patients" subtitle={compact ? undefined : "Scoped to the current persona's panel"} flush>
      <div style={{ padding: "0 var(--space-5) var(--space-3)" }}>
        <label className="sr-only" htmlFor="patient-search">Search patients</label>
        <input id="patient-search" className="input" placeholder="Search name, id, condition…" value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      {patients.loading && <div className="card-body"><Skeleton /></div>}
      {patients.error && <ErrorState error={patients.error} onRetry={patients.reload} />}
      {patients.data && filtered.length === 0 && <EmptyState title="No matching patients" />}
      <div className="list">
        {filtered.map((p) => (
          <button key={p.patient_id} type="button" className="list-item" aria-current={p.patient_id === patientId} onClick={() => setPatientId(p.patient_id)}>
            <span className="avatar" aria-hidden="true">{p.display_name.slice(0, 1)}</span>
            <div style={{ minWidth: 0 }}>
              <div className="item-title">{p.display_name.replace(" (synthetic record)", "")}</div>
              <div className="item-meta">
                <span className="mono">{p.patient_id}</span> · {p.gender}, {p.age_at_last_encounter ?? "?"}y
              </div>
              {!compact && <div className="item-meta">{p.conditions.slice(0, 2).join(" · ")}</div>}
            </div>
          </button>
        ))}
      </div>
    </Card>
  );
}

function DocumentViewer({ patientId, doc, onClose }: { patientId: string; doc: DocumentMeta; onClose: () => void }) {
  const detail = useApi(() => api.document(patientId, doc.id), `doc:${patientId}:${doc.id}`);
  return (
    <Drawer title={doc.title} subtitle={`${doc.type} · ${formatDate(doc.date, true)} · DocumentReference/${doc.id} v${doc.source.version}`} onClose={onClose}>
      {detail.loading && <Skeleton lines={8} />}
      {detail.error && <ErrorState error={detail.error} onRetry={detail.reload} />}
      {detail.data && (
        <>
          {detail.data.chunks.some((c) => c.flags.length > 0) && (
            <div className="banner warning">This document contains instruction-like text. The agent treats it as data and never follows it.</div>
          )}
          <div className="doc-text">{detail.data.text}</div>
          <div>
            <div className="field-label" style={{ marginBottom: 6 }}>Retrieval chunks ({detail.data.chunks.length})</div>
            <div className="stack">
              {detail.data.chunks.map((chunk) => (
                <div key={chunk.chunk_id} className="row" style={{ fontSize: 13 }}>
                  <span className="mono">{chunk.chunk_id}</span>
                  <span className="muted">{chunk.section}</span>
                  {chunk.flags.map((f) => <Badge key={f} tone="warning">{humanize(f)}</Badge>)}
                </div>
              ))}
            </div>
          </div>
          {detail.data.provenance && (
            <dl className="kv">
              {Object.entries(detail.data.provenance).map(([key, value]) => (
                <div key={key} style={{ display: "contents" }}>
                  <dt>{humanize(key)}</dt>
                  <dd className="mono">{String(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </>
      )}
    </Drawer>
  );
}

function PatientRecord({ data }: { data: PatientDetail }) {
  const [tab, setTab] = useState<Tab>("timeline");
  const [doc, setDoc] = useState<DocumentMeta | null>(null);
  const timeline = useApi(() => api.timeline(data.profile.patient_id), `timeline:${data.profile.patient_id}`);
  const p = data.profile;
  const kindLabel = useMemo(() => ({ laboratory: "Lab", "vital-signs": "Vital", "social-history": "Social" }) as Record<string, string>, []);

  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <section className="card">
        <div className="card-body">
          <div className="row-between">
            <div className="row" style={{ gap: 14 }}>
              <span className="avatar" style={{ width: 46, height: 46, fontSize: 16 }} aria-hidden="true">{p.display_name.slice(0, 1)}</span>
              <div>
                <h1 className="page-title" style={{ fontSize: "1.35rem" }}>{p.display_name.replace(" (synthetic record)", "")}</h1>
                <div className="row muted" style={{ fontSize: 13 }}>
                  <span className="mono">{p.patient_id}</span>
                  <span>·</span>
                  <span>{p.gender}, born {p.birth_date}</span>
                  <span>·</span>
                  <span>age {p.age_at_last_encounter ?? "?"} at last encounter</span>
                </div>
              </div>
            </div>
            <div className="row">
              <Badge tone="warning" dot>Synthetic record</Badge>
              <button type="button" className="btn primary sm" onClick={() => navigate("agent", { patient: p.patient_id })}>
                <Icons.agent size={15} /> Ask the agent
              </button>
            </div>
          </div>
          <div className="row faint" style={{ fontSize: 12, marginTop: 10 }}>
            <span>{p.freshness.current_resource_count} current FHIR resources</span>
            <span>·</span>
            <span>latest record update {formatDate(p.freshness.latest_record_update)}</span>
            <span>·</span>
            <span>ingested {formatDate(p.freshness.last_ingested_at, true)}</span>
          </div>
        </div>
      </section>

      {data.missing_information.length > 0 && (
        <div className="banner warning">
          <Icons.alert size={18} />
          <div>
            <strong>{data.missing_information.length} documentation gap{data.missing_information.length === 1 ? "" : "s"} detected.</strong>{" "}
            <button type="button" className="btn ghost sm" onClick={() => setTab("quality")}>Review gaps</button>
          </div>
        </div>
      )}

      <section className="card">
        <div style={{ padding: "0 var(--space-5)" }}>
          <Tabs<Tab>
            label="Record sections"
            value={tab}
            onChange={setTab}
            tabs={[
              { value: "timeline", label: "Timeline", count: timeline.data?.length },
              { value: "conditions", label: "Conditions", count: data.conditions.length },
              { value: "medications", label: "Medications", count: data.medications.length },
              { value: "labs", label: "Labs & vitals", count: data.lab_trends.length },
              { value: "notes", label: "Notes", count: data.documents.length },
              { value: "quality", label: "Data quality", count: data.missing_information.length },
            ]}
          />
        </div>
        <div className="card-body">
          {tab === "timeline" && (
            <>
              {timeline.loading && <Skeleton lines={6} />}
              {timeline.error && <ErrorState error={timeline.error} onRetry={timeline.reload} />}
              <div className="timeline">
                {(timeline.data ?? []).map((event, index) => (
                  <div className="timeline-item" key={`${event.source.reference}-${index}`}>
                    <div className="timeline-date">{formatDate(event.when)}</div>
                    <div className="timeline-rail"><span className={`timeline-node ${event.kind}`} /></div>
                    <div>
                      <div className="timeline-label">{event.label}</div>
                      <div className="faint" style={{ fontSize: 12 }}>
                        {kindLabel[event.kind] ?? humanize(event.kind)} · <span className="mono">{event.source.reference}</span> v{event.source.version}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
          {tab === "conditions" && (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Condition</th><th>ICD-10</th><th>Status</th><th>Onset</th><th>Source</th></tr></thead>
                <tbody>
                  {data.conditions.map((c) => (
                    <tr key={c.id}>
                      <td>{c.display}</td><td className="mono">{c.code}</td><td><Badge tone="info">{c.clinical_status}</Badge></td>
                      <td className="nowrap">{formatDate(c.onset)}</td><td className="mono faint">{c.source.reference}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {tab === "medications" && (
            data.medications.length === 0 ? <EmptyState title="No medication orders recorded">This is flagged as a documentation gap.</EmptyState> : (
              <div className="stack">
                {data.medications.map((m) => (
                  <div key={m.id} className="card" style={{ boxShadow: "none" }}>
                    <div className="card-body stack" style={{ gap: 8 }}>
                      <div className="row-between">
                        <span className="item-title">{m.medication}</span>
                        <Badge tone={m.status === "active" ? "success" : "neutral"}>{m.status}</Badge>
                      </div>
                      <span className="muted" style={{ fontSize: 13 }}>{m.dosage}</span>
                      {m.history.length > 1 && (
                        <div className="steps">
                          {m.history.map((v) => (
                            <div className="step" key={v.source.version}>
                              <span className="step-status skip" aria-hidden="true" />
                              <span style={{ fontSize: 13 }}>v{v.source.version}: {v.dosage} · {v.status}{v.notes.length ? ` — ${v.notes.join(" ")}` : ""}</span>
                              <span className="faint nowrap">{formatDate(v.source.version === "1" ? v.authored_on : v.source.last_updated)}</span>
                            </div>
                          ))}
                        </div>
                      )}
                      {m.history.length === 1 && m.notes.length > 0 && <span className="faint" style={{ fontSize: 12 }}>{m.notes.join(" ")}</span>}
                      <span className="faint mono" style={{ fontSize: 11 }}>{m.source.reference} · current v{m.source.version}</span>
                    </div>
                  </div>
                ))}
              </div>
            )
          )}
          {tab === "labs" && (
            <div className="stack" style={{ gap: "var(--space-5)" }}>
              <div className="grid cols-2">
                {data.lab_trends.map((series) => (
                  <div key={series.loinc} className="card" style={{ boxShadow: "none" }}>
                    <div className="card-body stack" style={{ gap: 6 }}>
                      <div className="row-between">
                        <span className="item-title">{series.name}</span>
                        <span className="row">
                          {series.unit ? <span className="faint" style={{ fontSize: 12 }}>{series.unit}</span> : <Badge tone="warning">no unit</Badge>}
                          {series.direction && <Badge tone={series.direction === "up" ? "warning" : "info"}>{series.direction} {series.change !== null ? `${series.change > 0 ? "+" : ""}${series.change}` : ""}</Badge>}
                        </span>
                      </div>
                      <LabTrendChart series={series} />
                    </div>
                  </div>
                ))}
              </div>
              {[...data.vitals, ...data.social_history].length > 0 && (
                <div className="table-wrap">
                  <table className="table">
                    <thead><tr><th>Observation</th><th>Value</th><th>Date</th><th>Category</th></tr></thead>
                    <tbody>
                      {[...data.vitals, ...data.social_history].map((o) => (
                        <tr key={o.id}><td>{o.name}</td><td>{o.value} {o.unit ?? ""}</td><td className="nowrap">{formatDate(o.effective)}</td><td>{o.category}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
          {tab === "notes" && (
            <div className="list" style={{ margin: "0 calc(-1 * var(--space-5))" }}>
              {data.documents.map((d) => (
                <button key={d.id} type="button" className="list-item" onClick={() => setDoc(d)}>
                  <Icons.file size={18} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className="item-title">{d.title}</div>
                    <div className="item-meta">{d.type} · {formatDate(d.date)} · {d.author}</div>
                  </div>
                  <span className="mono faint" style={{ fontSize: 12 }}>v{d.source.version}</span>
                </button>
              ))}
            </div>
          )}
          {tab === "quality" && (
            data.missing_information.length === 0 ? <EmptyState title="No documentation gaps detected">The completeness rules found nothing missing.</EmptyState> : (
              <div className="stack">
                {data.missing_information.map((f, i) => (
                  <div key={`${f.category}-${i}`} className="row-between" style={{ padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
                    <div>
                      <div style={{ fontSize: 14 }}>{f.message}</div>
                      <div className="faint mono" style={{ fontSize: 12 }}>{f.source.reference}</div>
                    </div>
                    <Badge tone={f.severity === "high" ? "danger" : "warning"}>{f.severity}</Badge>
                  </div>
                ))}
              </div>
            )
          )}
        </div>
      </section>
      {doc && <DocumentViewer patientId={p.patient_id} doc={doc} onClose={() => setDoc(null)} />}
    </div>
  );
}

export function PatientsPage() {
  const { patientId } = useApp();
  const detail = useApi(() => api.patient(patientId), `patient:${patientId}`);
  return (
    <div className="split">
      <PatientList />
      <div style={{ minWidth: 0 }}>
        {detail.loading && <Card><Skeleton lines={8} /></Card>}
        {detail.error && <Card><ErrorState error={detail.error} onRetry={detail.reload} /></Card>}
        {detail.data && <PatientRecord data={detail.data} />}
      </div>
    </div>
  );
}
