import { useEffect, useState } from "react";

export const BETA_ROUTES = [
  { name: "overview", label: "Overview", description: "MOCK serving state, coverage and warnings" },
  { name: "patients", label: "Patients", description: "Synthetic records, timeline, notes" },
  { name: "agent", label: "Case Review Agent", description: "Grounded questions and answers" },
  { name: "risk", label: "Risk & Explainability", description: "Readmission model and SHAP" },
  { name: "evidence", label: "Evidence Explorer", description: "Evidence returned for the latest MOCK answer" },
  { name: "evaluation", label: "Evaluation", description: "Offline quality and safety gates" },
  { name: "operations", label: "Operations", description: "Latency, errors, tools" },
  { name: "governance", label: "Governance", description: "Reviews, access control, audit" },
] as const;

export type BetaRouteName = (typeof BETA_ROUTES)[number]["name"];

export function parseRoute(hash: string): BetaRouteName {
  // The hash never carries patient IDs, evidence, questions or approval.
  const path = hash.replace(/^#\/?/, "").split("?")[0] ?? "";
  return BETA_ROUTES.find((route) => route.name === path)?.name ?? "patients";
}

export function navigateBeta(name: BetaRouteName): void {
  window.location.hash = `/${name}`;
}

export function useBetaRoute(): BetaRouteName {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}
