import type { ReadyExplorerData } from "./types";

/**
 * Representative payload used by unit tests and by `npm run dev` when the page
 * is opened directly (outside Streamlit). Values mirror the shape of a real
 * synthetic prediction; they are not used by the deployed dashboard.
 */
export const SAMPLE_EXPLORER_DATA: ReadyExplorerData = {
  status: "ready",
  patientId: "synthetic_014",
  probability: 0.4123,
  threshold: 0.3,
  riskLabel: "high",
  displayRiskLabel: "High",
  modelVersion: "dev-fixture",
  requestId: "dev-request-0001",
  features: [
    { feature: "Inpatient visits", contribution: 0.2312 },
    { feature: "Number of diagnoses", contribution: 0.0714 },
    { feature: "Age bracket", contribution: 0.0421 },
    { feature: "Insulin status: Up", contribution: -0.0188 },
    { feature: "Lab procedures", contribution: -0.0356 },
  ],
  disclaimer: "This is decision-support only and not medical advice.",
};
