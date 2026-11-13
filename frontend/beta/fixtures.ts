// Synthetic fixtures in the exact shape of the Beta record contract. Used only by the fixture client (unit tests,
// design review without a backend). Nothing here is real patient data and nothing here is fetched.
import type {
  BetaView,
  CoverageState,
  ObservationItem,
  OperationCounts,
  PatientChoice,
  SourceRef,
  Warning,
} from "./contract";

export const PATIENT_CHOICES: PatientChoice[] = [
  { alias: "mock-alpha", label: "MOCK Alpha" },
  { alias: "mock-bravo", label: "MOCK Bravo" },
];

const ref = (reference: string): SourceRef => ({ reference, version: "1", last_updated: "2026-11-06T15:00:00Z", synthetic: true });

const ops = (matches: number, importState = "imported"): OperationCounts => ({
  execution: "succeeded",
  import: matches === 0 ? "returned_none" : importState,
  reason: importState === "not_imported" ? "note_bodies_outside_policy" : null,
  matches,
  pages: 1,
  unique: matches,
  imported: matches === 0 || importState === "not_imported" ? 0 : matches,
  excluded: importState === "not_imported" ? matches : 0,
  rejected: 0,
  duplicates: 0,
});

const notRequested = (): OperationCounts => ({
  execution: "not_requested",
  import: "not_imported",
  reason: "outside_initial_policy",
  matches: 0,
  pages: 0,
  unique: 0,
  imported: 0,
  excluded: 0,
  rejected: 0,
  duplicates: 0,
});

function operations(counts: Record<string, number>): Record<string, OperationCounts> {
  const result: Record<string, OperationCounts> = { Patient: ops(1) };
  for (const [name, matches] of Object.entries(counts)) {
    result[name] = ops(matches, name === "DocumentReference:clinical-note" ? "not_imported" : "imported");
  }
  return result;
}

const lab = (name: string, value: number, unit: string, date: string, id: string): ObservationItem => ({
  name,
  loinc: null,
  value,
  unit,
  unit_missing: false,
  effective: date,
  source: ref(`Observation/${id}`),
});

const WARNINGS: Warning[] = [
  { code: "synthetic_mock", severity: "info", message: "Synthetic MOCK data from an in-process transport. This is not Epic data and not a real patient." },
  { code: "note_bodies_not_imported", severity: "warning", message: "Note bodies are never imported; only note metadata is shown." },
  { code: "medications_not_imported", severity: "warning", message: "Medications are not imported, so their absence here is not evidence of absence." },
];

function build(alias: string, label: string, gid: string, record: BetaView["record"], counts: Record<string, number>, withNotes: boolean): BetaView {
  const sources: Record<string, CoverageState> = {
    MedicationRequest: "not_imported",
    DocumentReference: "not_imported",
    "DocumentReference.metadata": withNotes ? "returned" : "returned_none",
    "DocumentReference.discharge_summary_metadata": withNotes ? "returned" : "returned_none",
  };
  const all = { ...operations(counts), MedicationRequest: notRequested(), Binary: notRequested() };
  return {
    mode: "MOCK",
    source: "epic-sandbox-mock",
    data_origin: "fixture",
    alias,
    label,
    serving: "fresh",
    active: { generation_id: gid, retrieved_at: 1793977200, promoted_at: 1793977205 },
    attempt: {
      state: "succeeded",
      policy: "mock-initial-r4-v1",
      error_code: null,
      requests: Object.keys(operations(counts)).length,
      operations: operations(counts),
      note_bodies: "not_imported",
      note_body_reason: "outside_initial_policy",
    },
    evidence_error: null,
    coverage: { sources, operations: all, unresolved_references: 0, note_bodies: "not_imported", field_status: {} },
    provenance: {
      generation_id: gid,
      manifest_version: "fixture",
      mapping_version: "fixture",
      policy: "mock-initial-r4-v1",
      retrieved_at: 1793977200,
      acquisition_started_at: 1793977190,
      bundle_sha256: null,
      resource_counts: {},
      mode: "MOCK",
      source: "epic-sandbox-mock",
    },
    record,
    warnings: structuredClone(WARNINGS),
  };
}

const section = <T>(items: T[]) => ({ items, total: items.length, truncated: false });

// Both patients carry the same upstream Observation id in the MOCK backend with different values;
// the distinct creatinine values here exist so the UI can show that records never cross patients.
export const VIEWS: Record<string, BetaView> = {
  "mock-alpha": build(
    "mock-alpha",
    "MOCK Alpha",
    "gen-fixture-alpha-1",
    {
      patient: { display_name: "MOCK Alpha (synthetic)", gender: "female", birth_date: "1962-04-09", age_at_last_encounter: 64, synthetic: true, source: ref("Patient/alpha") },
      conditions: section([
        { display: "Essential hypertension (synthetic)", category: "problem-list-item", clinical_status: "active", onset: "2025-03-02", source: ref("Condition/a1") },
        { display: "Acute bronchitis (synthetic)", category: "encounter-diagnosis", clinical_status: "resolved", onset: "2026-10-12", source: ref("Condition/a2") },
      ]),
      encounters: section([
        { class: "IMP", class_display: "Inpatient", start: "2026-10-12", end: "2026-10-15", length_of_stay_days: 3, reason: "Cough and fever (synthetic)", status: "finished", source: ref("Encounter/a1") },
      ]),
      labs: section([lab("Creatinine", 1.2, "mg/dL", "2026-10-13", "a-cr")]),
      vitals: section([lab("Heart rate", 88, "/min", "2026-10-12", "a-hr")]),
      reports: section([]),
      notes: section([{ type: "Discharge Summary", title: "Discharge summary", date: "2026-10-15", category: "clinical-note", status: "current" }]),
    },
    { Encounter: 1, "Condition:problem-list-item": 1, "Condition:encounter-diagnosis": 1, "Observation:laboratory": 1, "Observation:vital-signs": 1, "DocumentReference:clinical-note": 1, DiagnosticReport: 0 },
    true,
  ),
  "mock-bravo": build(
    "mock-bravo",
    "MOCK Bravo",
    "gen-fixture-bravo-1",
    {
      patient: { display_name: "MOCK Bravo (synthetic)", gender: "male", birth_date: "1951-11-30", age_at_last_encounter: 74, synthetic: true, source: ref("Patient/bravo") },
      conditions: section([
        { display: "Chronic kidney disease, stage 4 (synthetic)", category: "problem-list-item", clinical_status: "active", onset: "2024-08-19", source: ref("Condition/b1") },
      ]),
      encounters: section([
        { class: "EMER", class_display: "Emergency", start: "2026-10-20", end: null, length_of_stay_days: null, reason: "Weakness and nausea (synthetic)", status: "finished", source: ref("Encounter/b1") },
      ]),
      labs: section([lab("Creatinine", 4.8, "mg/dL", "2026-10-20", "b-cr")]),
      vitals: section([]),
      reports: section([]),
      notes: section([]),
    },
    { Encounter: 1, "Condition:problem-list-item": 1, "Condition:encounter-diagnosis": 0, "Observation:laboratory": 1, "Observation:vital-signs": 0, "DocumentReference:clinical-note": 0, DiagnosticReport: 0 },
    false,
  ),
};
