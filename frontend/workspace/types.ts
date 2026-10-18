// Typed contracts for the /api/v2 clinical workspace API (mirrors api/v2.py responses).

export interface SourceRef {
  reference: string;
  version: string | null;
  last_updated: string | null;
  synthetic: boolean;
}

export interface Persona {
  persona_id: string;
  display_name: string;
  role: "clinician" | "reviewer" | "admin";
  panel: string[];
  description: string;
}

export interface SystemStatus {
  service: string;
  version: string;
  status: string;
  uptime_s: number;
  database: { engine: string; schema_version: number };
  data: {
    patients: number;
    origin: string;
    startup_ingestion: { received: number; inserted: number; duplicates: number; invalid: number; status: string };
  };
  retrieval: RetrievalStats;
  readmission_model: { loaded: boolean; metadata: ModelSummary | null };
  inference: { mode: "llm" | "deterministic"; provider: ProviderInfo | null; label: string };
  evaluation: { run_at: string; dataset_version: string; gate_passed: boolean; mode: string } | null;
  disclaimer: string;
}

export interface ProviderInfo {
  provider: string;
  model?: string;
  circuit?: string;
  calls?: number;
  failures?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  mode?: string;
}

export interface RetrievalStats {
  index_version: string;
  embedder: string | null;
  patients: number;
  chunks: number;
  cache_hits: number;
  cache_misses: number;
  built_at: number | null;
}

export interface ModelSummary {
  model_type: string;
  model_version: string;
  training_date: string;
  risk_threshold: number;
  test_metrics: Record<string, number | null>;
  evaluation_protocol: string;
  calibration?: { model_version: string; dataset: string; brier: number; ece: number; prevalence: number; mean_probability: number; rows: number } | null;
}

export interface PatientListItem {
  patient_id: string;
  display_name: string;
  gender: string;
  age_at_last_encounter: number | null;
  conditions: string[];
  counts: Record<string, number>;
  freshness: { latest_record_update: string | null; last_ingested_at: string | null; current_resource_count: number };
}

export interface Encounter {
  id: string;
  class: string;
  class_display: string;
  start: string | null;
  end: string | null;
  length_of_stay_days: number | null;
  reason: string;
  admit_source: string;
  status: string;
  source: SourceRef;
}

export interface Condition {
  id: string;
  code: string;
  display: string;
  clinical_status: string;
  onset: string | null;
  source: SourceRef;
}

export interface MedicationVersion {
  id: string;
  medication: string;
  status: string;
  dosage: string | null;
  authored_on: string | null;
  notes: string[];
  source: SourceRef;
}

export interface Medication extends MedicationVersion {
  history: MedicationVersion[];
}

export interface LabPoint {
  effective: string;
  value: number;
  unit: string | null;
  source: SourceRef;
}

export interface LabSeries {
  loinc: string;
  name: string;
  unit: string | null;
  points: LabPoint[];
  count: number;
  direction: "up" | "down" | "flat" | null;
  change: number | null;
}

export interface Observation {
  id: string;
  name: string;
  value: number | string;
  unit: string | null;
  effective: string;
  category: string | null;
  source: SourceRef;
}

export interface DocumentMeta {
  id: string;
  type: string;
  title: string;
  date: string;
  author: string | null;
  encounter: string | null;
  source: SourceRef;
}

export interface Finding {
  category: string;
  severity: string;
  message: string;
  source: SourceRef;
}

export interface PatientDetail {
  profile: {
    patient_id: string;
    display_name: string;
    gender: string;
    birth_date: string;
    age_at_last_encounter: number | null;
    synthetic: boolean;
    counts: Record<string, number>;
    freshness: PatientListItem["freshness"];
  };
  encounters: Encounter[];
  conditions: Condition[];
  medications: Medication[];
  lab_trends: LabSeries[];
  vitals: Observation[];
  social_history: Observation[];
  documents: DocumentMeta[];
  missing_information: Finding[];
}

export interface TimelineEvent {
  when: string | null;
  kind: string;
  label: string;
  source: SourceRef;
}

export interface Chunk {
  chunk_id: string;
  patient_id: string;
  source_ref: string;
  source_version: string;
  source_type: string;
  title: string;
  section: string;
  date: string;
  text: string;
  flags: string[];
}

export interface DocumentDetail extends DocumentMeta {
  text: string;
  provenance: Record<string, string | number> | null;
  chunks: Chunk[];
}

export interface RetrievalHit extends Chunk {
  scores: { fused: number; bm25: number; bm25_rank: number | null; dense: number; dense_rank: number | null };
}

export interface RetrievalResponse {
  patient_id: string;
  query: string;
  mode: string;
  results: RetrievalHit[];
  diagnostics: {
    index_version: string;
    embedder: string | null;
    fusion: string;
    partition_size: number;
    latency_ms: number;
    cache_hit: boolean;
  };
}

export interface FeatureContribution {
  feature: string;
  contribution: number;
}

export interface RiskResponse {
  available: boolean;
  patient_id: string;
  reasons?: string[];
  encounter_id?: string;
  prediction?: {
    risk_probability: number;
    readmission_risk: string;
    risk_threshold: number;
    model_version: string;
    top_features: FeatureContribution[];
  };
  features?: Record<string, string | number | boolean>;
  caveats?: string[];
  model?: ModelSummary;
  disclaimer?: string;
}

export interface Claim {
  text: string;
  citations: string[];
  kind: string;
  lexical_support?: number;
  reasons?: string[];
}

export interface Evidence {
  evidence_id: string;
  patient_id: string;
  source_ref: string;
  source_version: string;
  text: string;
  kind: string;
  title: string;
  date: string;
  flags: string[];
  scores: RetrievalHit["scores"] | null;
  cited: boolean;
}

export interface ToolCall {
  tool: string;
  ok: boolean;
  evidence_count: number;
  error_type: string | null;
  error: string | null;
  latency_ms: number;
  args: Record<string, unknown>;
}

export interface TraceSpan {
  node: string;
  duration_ms: number;
  [key: string]: unknown;
}

export type Outcome = "answered" | "abstained" | "refused" | "cancelled" | "error";

export interface AgentRun {
  run_id: string;
  session_id: string;
  patient_id: string;
  question: string;
  outcome: Outcome;
  answer: {
    summary: string | null;
    claims: Claim[];
    rejected_claims: Claim[];
    uncertainty: string[];
    abstain_reason: string | null;
  };
  refusal: { category: string; message: string } | null;
  risk: {
    available: boolean;
    reasons?: string[];
    encounter_id?: string;
    risk_probability?: number;
    risk_label?: string;
    risk_threshold?: number;
    model_version?: string;
    top_features?: FeatureContribution[];
    caveats?: string[];
  } | null;
  evidence: Evidence[];
  plan: { source: string; intents: string[]; notes: string[]; calls: { name: string; args: Record<string, unknown> }[] } | null;
  tool_calls: ToolCall[];
  verification: {
    status: string;
    claims_total: number;
    claims_supported: number;
    supported_ratio: number;
    isolation_violations: number;
    summary_verified?: boolean;
  } | null;
  review: { required: boolean; reasons: string[] };
  inference: {
    mode: "llm" | "deterministic";
    planner: string | null;
    composer: string | null;
    fallback_used: boolean;
    llm_attempt: { status: string; claims_total: number; claims_supported: number; rejected: Claim[]; abstain_reason?: string | null; notes?: string[] } | null;
    provider: ProviderInfo | null;
    degraded_reason?: "quota_exhausted" | "relay_unavailable" | "provider_unavailable" | null;
    label: string;
  };
  trace: TraceSpan[];
  error: string | null;
  latency_ms: number;
  created_at: string;
  disclaimer: string;
}

export interface RecentRun {
  run_id: string;
  session_id: string;
  patient_id: string;
  question: string;
  outcome: Outcome;
  inference_mode: string;
  latency_ms: number;
  created_at: string;
}

export interface ReviewEvent {
  actor: string;
  action: string;
  from_status: string | null;
  to_status: string;
  artifact_hash: string;
  comment: string | null;
  created_at: string;
}

export interface Review {
  review_id: string;
  artifact_id: string;
  artifact_version: number;
  artifact_hash: string;
  patient_id: string;
  status: "pending_review" | "approved" | "rejected" | "needs_revision" | "superseded";
  submitted_by: string;
  created_at: string;
  updated_at: string;
  approval_valid: boolean;
  artifact?: {
    version: number;
    content_hash: string;
    content: {
      question: string;
      summary: string | null;
      claims: { text: string; citations: string[] }[];
      uncertainty: string[];
      risk: AgentRun["risk"];
      inference_mode: string;
      reviewer_note: string | null;
      run_id: string;
    };
  } | null;
  content_visible?: boolean;
  events?: ReviewEvent[];
}

export interface AuditEvent {
  event_id: string;
  ts: string;
  persona: string;
  role: string;
  action: string;
  resource: string;
  decision: "allow" | "deny";
  reason: string;
}

export interface PolicyMatrix {
  patients: string[];
  rows: { persona: string; role: string; action: string; patient_scoped: boolean; decisions: Record<string, boolean> }[];
  note: string;
}

export interface Percentiles {
  p50_ms: number | null;
  p95_ms: number | null;
}

export interface OpsMetrics {
  generated_at: string;
  window: { requests: number; agent_runs: number };
  routes: ({ route: string; count: number; error_rate: number; client_error_rate: number } & Percentiles)[];
  agent: { outcomes: Record<string, number>; modes: Record<string, number> } & Percentiles;
  tools: ({ tool: string; calls: number; errors: number } & Percentiles)[];
  nodes: ({ node: string } & Percentiles)[];
  retrieval: RetrievalStats;
  provider: ProviderInfo;
  rate_limit: { requests_per_minute: number; agent_runs_per_minute: number; rejected_since_start: number };
  note: string;
}

export interface EvalSummary {
  cases: number;
  passed: number;
  outcome_accuracy: number | null;
  answer_correctness: number | null;
  tool_selection_correctness: number | null;
  schema_validity: number | null;
  citation_coverage: number | null;
  citation_precision: number | null;
  evidence_recall: number | null;
  groundedness: number | null;
  unsupported_claim_rate: number;
  safety_adherence: number | null;
  isolation_violations: number;
  authorization_violations: number;
  unsafe_output_count: number;
  fallback_rate: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
}

export interface EvalCase {
  id: string;
  split: string;
  category: string;
  critical: boolean;
  passed: boolean;
  outcome: string;
  latency_ms: number;
  failed_checks?: string[];
  answer_excerpt?: string;
}

export interface RetrievalModeMetrics {
  recall_at_5: number | null;
  precision_at_5: number | null;
  mrr_at_10: number | null;
  ndcg_at_10: number | null;
  latency_p95_ms: number | null;
  queries: number;
}

export interface EvaluationReport {
  run_at: string;
  git_commit: string | null;
  dataset_version: string;
  mode: string;
  provider: ProviderInfo | null;
  scoring: string;
  summary: Record<"dev" | "heldout" | "all", EvalSummary>;
  retrieval: {
    modes: Record<string, Record<"dev" | "heldout" | "all", RetrievalModeMetrics>>;
    isolation: { probes: number; violations: number };
    embedder: string;
  };
  gate: { passed: boolean; failures: string[]; thresholds_version: string };
  cases: EvalCase[];
  history: { run_at: string; git_commit: string | null; mode: string; model: string | null; gate_passed: boolean; outcome_accuracy: number | null; answer_correctness: number | null; safety_adherence: number | null; latency_p50_ms: number | null }[];
  comparisons: { file: string; run_at: string; mode: string; provider: ProviderInfo | null; gate: EvaluationReport["gate"]; summary: Record<"dev" | "heldout" | "all", EvalSummary>; failed_cases: EvalCase[] }[];
}

export interface ApiErrorBody {
  error?: { code: string; message: string; request_id?: string; scope?: "visitor" | "deployment";
    limit_kind?: "agent_runs" | "api_requests"; retry_after_s?: number; resets_at?: string };
  detail?: unknown;
}
