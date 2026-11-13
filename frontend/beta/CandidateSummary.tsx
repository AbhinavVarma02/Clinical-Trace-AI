import type { CandidateResult } from "./genuine";
import { Card, Disclosure, humanize } from "./ui";

const TITLES = {
  acquire: "Epic acquisition staged",
  review: "Verified candidate review",
  promote: "Candidate promoted",
};

export function CandidateSummary({ candidate }: { candidate: CandidateResult }) {
  const { review, provenance } = candidate;
  const retrieved = new Date(review.retrieved_at * 1000).toISOString();
  const summarize = (values: Record<string, number | string>) => Object.entries(values).map(([key, count]) => `${key} ${count}`).join(" · ") || "—";
  return (
    <Card title={TITLES[candidate.operation]} subtitle="Provider-acquired Epic sandbox data. LIVE serving and clinical AI are disabled in this phase.">
      <div className="stack">
        <dl className="kv">
          <dt>Patient name</dt><dd>{review.patient_display_name ?? "Not returned"}</dd>
          <dt>FHIR patient ID</dt><dd className="mono">{candidate.patient_identifier}</dd>
          <dt>Approved alias</dt><dd className="mono">{candidate.alias}</dd>
          <dt>Source</dt><dd>{review.source}</dd>
          <dt>Policy</dt><dd>{review.policy}</dd>
          <dt>Retrieved (UTC)</dt><dd><time className="mono" dateTime={retrieved}>{retrieved}</time></dd>
          <dt>Attempt</dt><dd className="mono">{provenance.attempt_id}</dd>
          <dt>Generation</dt><dd className="mono">{provenance.generation_id}</dd>
          <dt>Manifest SHA-256</dt><dd className="mono" style={{ overflowWrap: "anywhere" }}>{provenance.manifest_sha256}</dd>
          <dt>Bundle SHA-256</dt><dd className="mono" style={{ overflowWrap: "anywhere" }}>{provenance.bundle_sha256}</dd>
          <dt>Resources</dt><dd>{summarize(review.resource_counts)}</dd>
          <dt>Completeness</dt><dd>{humanize(review.completeness)}</dd>
          <dt>Unresolved references</dt><dd>{review.unresolved_references}</dd>
          <dt>Note bodies</dt><dd>{humanize(review.note_bodies)}</dd>
          <dt>Document metadata</dt><dd>{review.document_metadata_count}</dd>
        </dl>
        <div className="table-wrap">
          <table className="table">
            <caption className="sr-only">Candidate source operations</caption>
            <thead>
              <tr><th scope="col">Operation</th><th scope="col">Execution</th><th scope="col">Import</th><th scope="col" className="num">Matches</th><th scope="col" className="num">Imported</th><th scope="col">Information codes</th></tr>
            </thead>
            <tbody>
              {Object.entries(review.operations).map(([name, counts]) => (
                <tr key={name}>
                  <td className="mono">{name}</td><td>{humanize(counts.execution ?? "unknown")}</td><td>{humanize(counts.import ?? "unknown")}</td>
                  <td className="num">{counts.matches ?? "—"}</td><td className="num">{counts.imported ?? "—"}</td><td>{counts.informational_codes?.join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Disclosure label="HTTP outcomes">
          <dl className="kv">
            {Object.entries(review.http_outcomes).map(([name, statuses]) => <Outcome key={name} name={name} value={statuses.join(", ") || "—"} />)}
          </dl>
        </Disclosure>
        <Disclosure label="Source last-updated timestamps">
          <dl className="kv">
            {Object.entries(review.source_timestamps).map(([kind, range]) => <Outcome key={kind} name={kind} value={`${range.count} timestamps · ${range.oldest} to ${range.newest}`} />)}
          </dl>
        </Disclosure>
        <Disclosure label="Mapping summary">
          <dl className="kv">
            <Outcome name="Mapping version" value={review.mapping.mapping_version} />
            <Outcome name="Received" value={summarize(review.mapping.received)} />
            <Outcome name="Mapped" value={summarize(review.mapping.mapped)} />
            <Outcome name="Rejected" value={summarize(review.mapping.rejected)} />
            <Outcome name="Field status" value={summarize(review.mapping.field_status)} />
            <Outcome name="Skipped types" value={summarize(review.mapping.skipped_types)} />
            <Outcome name="Dangling references" value={String(review.mapping.dangling_references)} />
          </dl>
        </Disclosure>
      </div>
    </Card>
  );
}

function Outcome({ name, value }: { name: string; value: string }) {
  return <><dt>{name}</dt><dd className="mono" style={{ overflowWrap: "anywhere" }}>{value}</dd></>;
}
