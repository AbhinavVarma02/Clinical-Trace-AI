import type { AgentRun } from "../types";

export function makeRun(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    run_id: "run-1234567890",
    session_id: "session-1234567890",
    patient_id: "synthetic_p001",
    question: "Why was metformin stopped?",
    outcome: "answered",
    answer: {
      summary: "1 verified statement(s) drawn from 1 source record(s).",
      claims: [{ text: "Metformin 1000 mg oral tablet is recorded as stopped.", citations: ["p001-m1@v2"], kind: "medication_status" }],
      rejected_claims: [],
      uncertainty: [],
      abstain_reason: null,
    },
    refusal: null,
    risk: null,
    evidence: [
      {
        evidence_id: "p001-m1@v2",
        patient_id: "synthetic_p001",
        source_ref: "MedicationRequest/p001-m1",
        source_version: "2",
        text: "Metformin 1000 mg oral tablet: stopped. Note: Stopped 2025-07-04 because of declining eGFR.",
        kind: "medication",
        title: "Metformin 1000 mg oral tablet",
        date: "2025-07-04T14:00:00Z",
        flags: [],
        scores: null,
        cited: true,
      },
    ],
    plan: { source: "deterministic-rules", intents: ["medications"], notes: [], calls: [{ name: "get_medication_history", args: {} }] },
    tool_calls: [{ tool: "get_medication_history", ok: true, evidence_count: 5, error_type: null, error: null, latency_ms: 3.2, args: {} }],
    verification: { status: "verified", claims_total: 1, claims_supported: 1, supported_ratio: 1, isolation_violations: 0, summary_verified: false },
    review: { required: false, reasons: [] },
    inference: {
      mode: "deterministic",
      planner: "deterministic-rules",
      composer: "deterministic-template",
      fallback_used: false,
      llm_attempt: null,
      provider: null,
      label: "Deterministic mode: rule-based planning and template/extractive answers; no language model was called.",
    },
    trace: [{ node: "guard", duration_ms: 0.4 }],
    error: null,
    latency_ms: 12.5,
    created_at: "2026-10-17T00:00:00Z",
    disclaimer: "This is decision-support only and not medical advice.",
    ...overrides,
  };
}
