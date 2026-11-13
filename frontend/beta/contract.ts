// Types of the Beta browser read contract (`GET /beta/epic/patients/{alias}/record`, built by api/beta_record.py).
// The fixture client returns the same shape, so the UI cannot depend on anything the server does not send.

export type Serving = "fresh" | "cached" | "stale" | "rolled_back" | "unavailable";
export type CoverageState = "returned" | "returned_none" | "not_imported";

export interface PatientChoice {
  alias: string;
  label: string;
  mode?: "LIVE";
  data_origin?: "synthetic_live_fixture";
}

export interface SourceRef {
  reference: string;
  version: string | null;
  last_updated: string | null;
  synthetic: boolean | null;
}

export interface Section<T> {
  items: T[];
  total: number;
  truncated: boolean;
}

export interface ConditionItem {
  display: string | null;
  category: string | null;
  clinical_status: string | null;
  onset: string | null;
  source: SourceRef;
}

export interface EncounterItem {
  class: string | null;
  class_display: string | null;
  start: string | null;
  end: string | null;
  length_of_stay_days: number | null;
  reason: string | null;
  status: string | null;
  source: SourceRef;
}

export interface ObservationItem {
  name: string;
  loinc: string | null;
  value: number | string | null;
  unit: string | null;
  unit_missing: boolean;
  effective: string;
  source: SourceRef;
}

export interface ReportItem {
  name: string | null;
  effective: string;
  conclusion: string | null;
  result_count: number;
  source: SourceRef;
}

export interface NoteItem {
  type: string | null;
  title: string | null;
  date: string | null;
  category: string | null;
  status: string | null;
  source?: SourceRef;
}

export interface BetaRecord {
  patient: {
    display_name: string | null;
    gender: string | null;
    birth_date: string | null;
    age_at_last_encounter: number | null;
    synthetic: boolean | null;
    source: SourceRef;
  };
  conditions: Section<ConditionItem>;
  encounters: Section<EncounterItem>;
  labs: Section<ObservationItem>;
  vitals: Section<ObservationItem>;
  reports: Section<ReportItem>;
  notes: Section<NoteItem>;
}

export interface OperationCounts {
  execution: string | null;
  import: string | null;
  reason: string | null;
  matches: number | null;
  pages: number | null;
  unique: number | null;
  imported: number | null;
  excluded: number | null;
  rejected: number | null;
  duplicates: number | null;
  informational_codes?: string[];
}

export interface Attempt {
  state: string;
  policy: string | null;
  error_code: string | null;
  requests: number | null;
  operations: Record<string, OperationCounts>;
  note_bodies: string;
  note_body_reason: string;
}

export interface Coverage {
  sources: Record<string, CoverageState>;
  operations: Record<string, OperationCounts>;
  unresolved_references: number;
  note_bodies: string;
  field_status: Record<string, number>;
  completeness?: "unconfirmed";
}

export interface Provenance {
  generation_id: string;
  attempt_id?: string;
  manifest_version: string | null;
  mapping_version: string | null;
  policy: string | null;
  retrieved_at: number | null;
  acquisition_started_at: number | null;
  bundle_sha256: string | null;
  resource_counts: Record<string, number>;
  mode: string | null;
  source: string | null;
  manifest_sha256?: string;
  completeness?: "unconfirmed";
  synthetic_transport?: boolean;
}

export interface Warning {
  code: string;
  severity: "info" | "warning";
  message: string;
}

export interface BetaView {
  mode: "MOCK" | "LIVE";
  source: string;
  data_origin: "synthetic_mock_transport" | "fixture" | "synthetic_live_fixture" | "epic_sandbox";
  alias: string;
  label: string;
  serving: Serving;
  freshness?: "fresh" | "cached" | "stale";
  publication?: "disabled";
  completeness?: "unconfirmed";
  active: { generation_id: string; retrieved_at: number; promoted_at: number } | null;
  attempt: Attempt | null;
  evidence_error: string | null;
  coverage: Coverage | null;
  provenance: Provenance | null;
  record: BetaRecord | null;
  warnings: Warning[];
}

// -- clinical answer (`POST .../generations/{id}/runs`) -------------------------------------------------------------
// Rendered exactly as the server verified it. The UI never adds a claim, a citation or a verification result.

export interface Claim {
  text: string;
  citations: string[];
  kind: string;
  computations?: { op: string; args: string[]; result: string }[];
}

export interface EvidenceItem {
  evidence_id: string;
  source_ref: string;
  source_version: string;
  title: string | null;
  text: string;
  date: string | null;
  kind: string;
  cited: boolean;
  flags: string[];
}

export interface Answer {
  run_id: string;
  question: string;
  outcome: string; // "answered" | "abstained" | "refused" (anything else is shown as-is)
  answer: { summary: string | null; claims: Claim[]; uncertainty: string[]; abstain_reason: string | null } | null;
  refusal: { category: string; message: string } | null;
  evidence: EvidenceItem[];
  verification: { status: string; claims_total: number; claims_supported: number; isolation_violations: number } | null;
  review: { required: boolean; reasons: string[] };
  inference: { mode: string; label: string };
  disclaimer: string;
  serving: Serving;
  error: unknown;
}
