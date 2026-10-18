import { useState, type FormEvent } from "react";
import { ApiError, api } from "../api";
import { EvidenceDrawer } from "../components/EvidencePanel";
import { Badge, Card, Disclosure, EmptyState, ErrorState, Segmented, formatDate, humanize, ms } from "../components/ui";
import { useApi, useApp } from "../state";
import type { Evidence, RetrievalResponse } from "../types";

type Mode = "hybrid" | "bm25" | "dense";

export function EvidencePage() {
  const { patientId, setPatientId } = useApp();
  const patients = useApi(api.patients, "patients");
  const [query, setQuery] = useState("why was the medication stopped");
  const [mode, setMode] = useState<Mode>("hybrid");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [result, setResult] = useState<RetrievalResponse | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState<Evidence | null>(null);

  async function search(event?: FormEvent) {
    event?.preventDefault();
    if (query.trim().length < 2) return;
    setLoading(true);
    setError(null);
    try {
      setResult(await api.retrieve(patientId, { query, k: 8, mode, date_from: dateFrom || undefined, date_to: dateTo || undefined }));
    } catch (err) {
      setError(err instanceof ApiError ? err : null);
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="page-title">Evidence Explorer</h1>
          <p className="page-subtitle">Search one patient's record and compare keyword, semantic and combined ranking.</p>
        </div>
      </div>
      <Card>
        <form className="grid cols-4" onSubmit={search} style={{ alignItems: "end" }}>
          <label className="field">
            <span className="field-label">Patient</span>
            <select className="select" value={patientId} onChange={(e) => setPatientId(e.target.value)}>
              {(patients.data ?? []).map((p) => <option key={p.patient_id} value={p.patient_id}>{p.patient_id}</option>)}
            </select>
          </label>
          <label className="field" style={{ gridColumn: "span 2" }}>
            <span className="field-label">Query</span>
            <input className="input" value={query} onChange={(e) => setQuery(e.target.value)} maxLength={300} />
          </label>
          <button type="submit" className="btn primary" disabled={loading}>{loading ? "Searching…" : "Search"}</button>
          <div className="field">
            <span className="field-label">Ranking</span>
            <Segmented<Mode> label="Ranking mode" value={mode} onChange={setMode} options={[{ value: "hybrid", label: "Hybrid" }, { value: "bm25", label: "BM25" }, { value: "dense", label: "Dense" }]} />
          </div>
          <label className="field">
            <span className="field-label">From date</span>
            <input className="input" type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">To date</span>
            <input className="input" type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
          </label>
        </form>
      </Card>
      {error && <Card><ErrorState error={error} onRetry={() => void search()} /></Card>}
      {result && (
        <Card
          title={`${result.results.length} results`}
          subtitle={`${result.diagnostics.partition_size} chunks searched · ${ms(result.diagnostics.latency_ms)}${result.diagnostics.cache_hit ? " (cache hit)" : ""}`}
          flush
        >
          {result.results.length === 0 ? <EmptyState title="No matching evidence">Try another query or widen the date range.</EmptyState> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>#</th><th>Source</th><th>Excerpt</th><th className="num">Fused</th><th className="num">BM25</th><th className="num">Dense</th></tr></thead>
                <tbody>
                  {result.results.map((hit, i) => (
                    <tr key={hit.chunk_id} style={{ cursor: "pointer" }} onClick={() => setOpen({
                      evidence_id: hit.chunk_id, patient_id: hit.patient_id, source_ref: hit.source_ref, source_version: hit.source_version,
                      text: hit.text, kind: "retrieved", title: `${hit.title} - ${hit.section}`, date: hit.date, flags: hit.flags, scores: hit.scores, cited: false,
                    })}>
                      <td className="num">{i + 1}</td>
                      <td style={{ minWidth: 180 }}>
                        <div style={{ fontWeight: 600 }}>{hit.title}</div>
                        <div className="faint" style={{ fontSize: 12 }}>{hit.section} · {formatDate(hit.date)}</div>
                        <div className="mono faint" style={{ fontSize: 11 }}>{hit.chunk_id}</div>
                        {hit.flags.map((f) => <Badge key={f} tone="warning">{humanize(f)}</Badge>)}
                      </td>
                      <td style={{ minWidth: 260 }}>{hit.text.length > 220 ? `${hit.text.slice(0, 220)}…` : hit.text}</td>
                      <td className="num mono">{hit.scores.fused.toFixed(4)}</td>
                      <td className="num mono">{hit.scores.bm25_rank ? `#${hit.scores.bm25_rank}` : "–"} <span className="faint">{hit.scores.bm25.toFixed(2)}</span></td>
                      <td className="num mono">#{hit.scores.dense_rank ?? "–"} <span className="faint">{hit.scores.dense.toFixed(3)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
      {result && (
        <Disclosure label="Retrieval diagnostics">
          <p className="mono muted" style={{ fontSize: 12, margin: 0 }}>
            {result.diagnostics.fusion} · embedder {result.diagnostics.embedder} · index {result.diagnostics.index_version} · partition {result.diagnostics.partition_size} chunks
          </p>
        </Disclosure>
      )}
      {!result && !error && <Card><EmptyState title="Search to see ranked evidence">Results stay inside this patient's record.</EmptyState></Card>}
      {open && <EvidenceDrawer evidence={open} patientId={patientId} onClose={() => setOpen(null)} />}
    </>
  );
}
