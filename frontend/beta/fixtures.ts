// Synthetic MOCK fixtures for the Epic Beta browser slice. Nothing here is real patient data and
// nothing here is fetched: aliases and labels mirror the fixed MOCK policy, and the status object
// mirrors the fields of the owner status route (`mode`, `source`, `serving`, `active`, `attempt`).
// Record content has no Beta route yet, so it is fixture-defined and clearly marked as such.

export type Serving = "fresh" | "cached" | "stale" | "rolled_back" | "unavailable";
export type CoverageState = "returned" | "returned_none" | "not_imported";

export interface PatientChoice {
  alias: string;
  label: string;
}

export interface FixtureCondition {
  display: string;
  category: "problem-list-item" | "encounter-diagnosis";
  recorded: string;
}

export interface FixtureEncounter {
  id: string;
  class: string;
  start: string;
  reason: string;
}

export interface FixtureLab {
  name: string;
  value: number;
  unit: string;
  date: string;
}

export interface FixtureNote {
  title: string;
  type: string;
  date: string;
}

export interface FixtureRecord {
  conditions: FixtureCondition[];
  encounters: FixtureEncounter[];
  labs: FixtureLab[];
  notes: FixtureNote[];
}

export interface OperationCounts {
  execution: "succeeded" | "not_requested";
  matches: number;
  pages: number;
  unique: number;
  imported: number;
  excluded: number;
  rejected: number;
  duplicates: number;
}

export interface FixtureStatus {
  mode: "MOCK";
  source: "epic-sandbox-mock";
  serving: Serving;
  active: { generation_id: string; retrieved_at: string; promoted_at: string } | null;
  attempt: {
    state: string;
    requests: number;
    operations: Record<string, OperationCounts>;
    note_bodies: "not_imported";
    note_body_reason: "outside_initial_policy";
  } | null;
  coverage: Record<string, CoverageState>;
}

export const SESSION_FIXTURE = {
  authenticated: true,
  mode: "MOCK",
  owner_label: "Authenticated MOCK owner",
} as const;

export const PATIENT_CHOICES: PatientChoice[] = [
  { alias: "mock-alpha", label: "MOCK Alpha" },
  { alias: "mock-bravo", label: "MOCK Bravo" },
];

const ops = (matches: number, imported = matches): OperationCounts => ({
  execution: "succeeded",
  matches,
  pages: 1,
  unique: matches,
  imported,
  excluded: matches - imported,
  rejected: 0,
  duplicates: 0,
});

function status(generation: string, counts: Record<string, number>, withNotes: boolean): FixtureStatus {
  const operations: Record<string, OperationCounts> = { Patient: ops(1) };
  for (const [name, matches] of Object.entries(counts)) operations[name] = ops(matches);
  return {
    mode: "MOCK",
    source: "epic-sandbox-mock",
    serving: "fresh",
    active: { generation_id: generation, retrieved_at: "2026-11-06T15:00:00Z", promoted_at: "2026-11-06T15:00:05Z" },
    attempt: {
      state: "succeeded",
      requests: Object.keys(operations).length,
      operations,
      note_bodies: "not_imported",
      note_body_reason: "outside_initial_policy",
    },
    coverage: {
      MedicationRequest: "not_imported",
      DocumentReference: "not_imported",
      "DocumentReference.metadata": withNotes ? "returned" : "returned_none",
      "DocumentReference.discharge_summary_metadata": withNotes ? "returned" : "returned_none",
    },
  };
}

// Both patients carry the same upstream Observation id in the MOCK backend with different values;
// the distinct creatinine values here exist so the UI can show that records never cross patients.
export const RECORDS: Record<string, FixtureRecord> = {
  "mock-alpha": {
    conditions: [
      { display: "Essential hypertension (synthetic)", category: "problem-list-item", recorded: "2025-03-02" },
      { display: "Acute bronchitis (synthetic)", category: "encounter-diagnosis", recorded: "2026-10-12" },
    ],
    encounters: [{ id: "enc-a1", class: "Inpatient", start: "2026-10-12", reason: "Cough and fever (synthetic)" }],
    labs: [{ name: "Creatinine", value: 1.2, unit: "mg/dL", date: "2026-10-13" }],
    notes: [{ title: "Discharge summary", type: "Discharge Summary", date: "2026-10-15" }],
  },
  "mock-bravo": {
    conditions: [{ display: "Chronic kidney disease, stage 4 (synthetic)", category: "problem-list-item", recorded: "2024-08-19" }],
    encounters: [{ id: "enc-b1", class: "Emergency", start: "2026-10-20", reason: "Weakness and nausea (synthetic)" }],
    labs: [{ name: "Creatinine", value: 4.8, unit: "mg/dL", date: "2026-10-20" }],
    notes: [],
  },
};

export const STATUSES: Record<string, FixtureStatus> = {
  "mock-alpha": status("gen-fixture-alpha-1", { Encounter: 1, "Condition:problem-list-item": 1, "Condition:encounter-diagnosis": 1, "Observation:laboratory": 1, "Observation:vital-signs": 0, "DocumentReference:clinical-note": 1, DiagnosticReport: 0 }, true),
  "mock-bravo": status("gen-fixture-bravo-1", { Encounter: 1, "Condition:problem-list-item": 1, "Condition:encounter-diagnosis": 0, "Observation:laboratory": 1, "Observation:vital-signs": 0, "DocumentReference:clinical-note": 0, DiagnosticReport: 0 }, false),
};
