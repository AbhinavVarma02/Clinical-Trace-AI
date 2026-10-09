/**
 * Pure, presentation-only helpers. Nothing here changes a model output: values
 * are validated, ranked, sorted, and formatted for display only.
 */

import type {
  BackendRiskLabel,
  DirectionFilter,
  DisplayRiskTier,
  ExplorerData,
  FeatureContribution,
  RankedContribution,
  SortMode,
} from "../types";

export const DEFAULT_DISCLAIMER = "This is decision-support only and not medical advice.";

const DISPLAY_TIERS: readonly DisplayRiskTier[] = ["High", "Moderate", "Low"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function parseFeatures(value: unknown): FeatureContribution[] {
  if (!Array.isArray(value)) return [];
  const features: FeatureContribution[] = [];
  for (const item of value) {
    if (!isRecord(item)) continue;
    const feature = text(item.feature).trim();
    const contribution = finiteNumber(item.contribution);
    if (feature && contribution !== null) features.push({ feature, contribution });
  }
  return features;
}

/**
 * Validate the untyped args Streamlit sends. Anything malformed degrades to
 * the empty state instead of throwing inside the iframe.
 */
export function parseExplorerData(raw: unknown): ExplorerData {
  const disclaimer = isRecord(raw) ? text(raw.disclaimer, DEFAULT_DISCLAIMER) || DEFAULT_DISCLAIMER : DEFAULT_DISCLAIMER;
  if (!isRecord(raw) || raw.status !== "ready") {
    const message = isRecord(raw) ? text(raw.message) : "";
    return message ? { status: "empty", disclaimer, message } : { status: "empty", disclaimer };
  }

  const probability = finiteNumber(raw.probability);
  const threshold = finiteNumber(raw.threshold);
  if (probability === null || threshold === null) {
    return { status: "empty", disclaimer, message: "Prediction result is incomplete." };
  }

  const riskLabel: BackendRiskLabel = text(raw.riskLabel).toLowerCase() === "high" ? "high" : "low";
  const tier = text(raw.displayRiskLabel) as DisplayRiskTier;

  return {
    status: "ready",
    patientId: text(raw.patientId, "synthetic patient"),
    probability,
    threshold,
    riskLabel,
    displayRiskLabel: DISPLAY_TIERS.includes(tier) ? tier : null,
    modelVersion: text(raw.modelVersion, "unknown"),
    requestId: text(raw.requestId),
    features: parseFeatures(raw.features),
    disclaimer,
  };
}

/** Attach rank, direction, and share-of-signal to each contribution. */
export function rankContributions(features: readonly FeatureContribution[]): RankedContribution[] {
  const total = features.reduce((sum, item) => sum + Math.abs(item.contribution), 0);
  const max = features.reduce((best, item) => Math.max(best, Math.abs(item.contribution)), 0);
  const byImpact = [...features].sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
  return byImpact.map((item, index) => {
    const magnitude = Math.abs(item.contribution);
    return {
      ...item,
      impactRank: index + 1,
      magnitude,
      direction: item.contribution >= 0 ? "raises" : "lowers",
      share: total > 0 ? magnitude / total : 0,
      relativeToMax: max > 0 ? magnitude / max : 0,
    };
  });
}

export function sortContributions(items: readonly RankedContribution[], mode: SortMode): RankedContribution[] {
  const sorted = [...items];
  switch (mode) {
    case "impact":
      return sorted.sort((a, b) => a.impactRank - b.impactRank);
    case "raises-first":
      return sorted.sort((a, b) => b.contribution - a.contribution);
    case "lowers-first":
      return sorted.sort((a, b) => a.contribution - b.contribution);
    case "alphabetical":
      return sorted.sort((a, b) => a.feature.localeCompare(b.feature));
  }
}

export function filterContributions(
  items: readonly RankedContribution[],
  filter: DirectionFilter,
): RankedContribution[] {
  return filter === "all" ? [...items] : items.filter((item) => item.direction === filter);
}

export function formatPercent(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatSigned(value: number, digits = 3): string {
  const fixed = Math.abs(value).toFixed(digits);
  return value >= 0 ? `+${fixed}` : `−${fixed}`;
}

export function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, value * 100));
}
