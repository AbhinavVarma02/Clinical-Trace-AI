// Evidence drawer: shows one cited evidence item and, for documents, the full source with provenance.
import { api } from "../api";
import { useApi } from "../state";
import type { Evidence } from "../types";
import { Badge, Drawer, ErrorState, Skeleton, formatDate } from "./ui";

function highlight(text: string, excerpt: string) {
  const needle = excerpt.trim().slice(0, 160);
  const index = needle ? text.indexOf(needle) : -1;
  if (index < 0) return text;
  return (
    <>
      {text.slice(0, index)}
      <mark>{text.slice(index, index + needle.length)}</mark>
      {text.slice(index + needle.length)}
    </>
  );
}

export function EvidenceDrawer({ evidence, patientId, onClose }: { evidence: Evidence; patientId: string; onClose: () => void }) {
  const isDocument = evidence.source_ref.startsWith("DocumentReference/");
  const documentId = evidence.source_ref.split("/")[1] ?? "";
  const doc = useApi(() => api.document(patientId, documentId), `doc:${patientId}:${documentId}`, isDocument);
  const body = evidence.text.includes("): ") ? evidence.text.split("): ").slice(1).join("): ") : evidence.text;
  return (
    <Drawer title={evidence.title || evidence.source_ref} subtitle={`${evidence.source_ref} · version ${evidence.source_version}`} onClose={onClose}>
      <div className="row">
        <Badge tone="info">{evidence.kind.replace(/_/g, " ")}</Badge>
        {evidence.cited && <Badge tone="success">cited in answer</Badge>}
        {evidence.flags.map((flag) => (
          <Badge key={flag} tone="warning">
            {flag.replace(/_/g, " ")}
          </Badge>
        ))}
      </div>
      <dl className="kv">
        <dt>Evidence id</dt>
        <dd className="mono">{evidence.evidence_id}</dd>
        <dt>Source</dt>
        <dd className="mono">{evidence.source_ref}</dd>
        <dt>Record date</dt>
        <dd>{formatDate(evidence.date, true)}</dd>
        {evidence.scores && (
          <>
            <dt>Retrieval</dt>
            <dd className="mono">
              fused {evidence.scores.fused.toFixed(4)} · bm25 #{evidence.scores.bm25_rank ?? "–"} ({evidence.scores.bm25.toFixed(2)}) · dense #
              {evidence.scores.dense_rank ?? "–"} ({evidence.scores.dense.toFixed(3)})
            </dd>
          </>
        )}
      </dl>
      <div>
        <div className="field-label" style={{ marginBottom: 6 }}>Evidence text given to the verifier</div>
        <div className="doc-text">{body}</div>
      </div>
      {evidence.flags.includes("possible_instruction_injection") && (
        <div className="banner warning" role="note">
          This source contains instruction-like text. It is treated strictly as record content: it was not followed, and those sentences were excluded from answers.
        </div>
      )}
      {isDocument && (
        <div>
          <div className="field-label" style={{ marginBottom: 6 }}>Full source document</div>
          {doc.loading && <Skeleton lines={6} />}
          {doc.error && <ErrorState error={doc.error} onRetry={doc.reload} />}
          {doc.data && (
            <div className="stack">
              <div className="doc-text">{highlight(doc.data.text, body)}</div>
              {doc.data.provenance && (
                <dl className="kv">
                  <dt>Generator</dt>
                  <dd className="mono">{String(doc.data.provenance.generator_source)}</dd>
                  <dt>Content hash</dt>
                  <dd className="mono">{String(doc.data.provenance.content_hash).slice(0, 16)}…</dd>
                  <dt>Ingested</dt>
                  <dd>{formatDate(String(doc.data.provenance.ingested_at), true)}</dd>
                  <dt>Ingestion run</dt>
                  <dd className="mono">{String(doc.data.provenance.ingestion_run_id).slice(0, 8)}</dd>
                </dl>
              )}
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}
