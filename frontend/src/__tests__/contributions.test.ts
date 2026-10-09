import { describe, expect, it } from "vitest";

import { SAMPLE_EXPLORER_DATA } from "../fixtures";
import {
  DEFAULT_DISCLAIMER,
  clampPercent,
  filterContributions,
  formatPercent,
  formatSigned,
  parseExplorerData,
  rankContributions,
  sortContributions,
} from "../lib/contributions";

const features = SAMPLE_EXPLORER_DATA.features;

describe("parseExplorerData", () => {
  it("accepts a well-formed ready payload unchanged", () => {
    expect(parseExplorerData({ ...SAMPLE_EXPLORER_DATA })).toEqual(SAMPLE_EXPLORER_DATA);
  });

  it("falls back to the empty state for missing or malformed args", () => {
    for (const raw of [undefined, null, 42, "x", [], {}, { status: "pending" }]) {
      const parsed = parseExplorerData(raw);
      expect(parsed.status).toBe("empty");
      expect(parsed.disclaimer).toBe(DEFAULT_DISCLAIMER);
    }
  });

  it("keeps the Python-provided disclaimer and message in the empty state", () => {
    expect(parseExplorerData({ status: "empty", disclaimer: "Custom disclaimer.", message: "No run yet." })).toEqual({
      status: "empty",
      disclaimer: "Custom disclaimer.",
      message: "No run yet.",
    });
  });

  it("treats non-finite probability or threshold as unavailable", () => {
    const parsed = parseExplorerData({ ...SAMPLE_EXPLORER_DATA, probability: null });
    expect(parsed.status).toBe("empty");
  });

  it("drops malformed feature rows without inventing values", () => {
    const parsed = parseExplorerData({
      ...SAMPLE_EXPLORER_DATA,
      features: [{ feature: "A", contribution: 0.1 }, { feature: "", contribution: 1 }, { feature: "B" }, null],
    });
    expect(parsed.status === "ready" && parsed.features).toEqual([{ feature: "A", contribution: 0.1 }]);
  });

  it("normalises the backend label and rejects unknown display tiers", () => {
    const parsed = parseExplorerData({ ...SAMPLE_EXPLORER_DATA, riskLabel: "HIGH", displayRiskLabel: "Extreme" });
    expect(parsed.status === "ready" && parsed.riskLabel).toBe("high");
    expect(parsed.status === "ready" && parsed.displayRiskLabel).toBeNull();
  });
});

describe("rankContributions", () => {
  const ranked = rankContributions(features);

  it("ranks by absolute contribution without altering values", () => {
    expect(ranked.map((item) => item.feature)).toEqual([
      "Inpatient visits",
      "Number of diagnoses",
      "Age bracket",
      "Lab procedures",
      "Insulin status: Up",
    ]);
    expect(ranked.map((item) => item.impactRank)).toEqual([1, 2, 3, 4, 5]);
    for (const item of ranked) {
      expect(features).toContainEqual({ feature: item.feature, contribution: item.contribution });
    }
  });

  it("derives direction, share, and relative magnitude", () => {
    const total = features.reduce((sum, item) => sum + Math.abs(item.contribution), 0);
    expect(ranked.reduce((sum, item) => sum + item.share, 0)).toBeCloseTo(1, 10);
    expect(ranked[0]?.relativeToMax).toBe(1);
    expect(ranked[0]?.share).toBeCloseTo(0.2312 / total, 10);
    expect(ranked.find((item) => item.feature === "Lab procedures")?.direction).toBe("lowers");
    expect(ranked.find((item) => item.feature === "Age bracket")?.direction).toBe("raises");
  });

  it("handles an empty list and all-zero contributions", () => {
    expect(rankContributions([])).toEqual([]);
    const zero = rankContributions([{ feature: "A", contribution: 0 }]);
    expect(zero[0]).toMatchObject({ share: 0, relativeToMax: 0, direction: "raises" });
  });
});

describe("sortContributions / filterContributions", () => {
  const ranked = rankContributions(features);

  it("sorts by each mode", () => {
    expect(sortContributions(ranked, "raises-first")[0]?.feature).toBe("Inpatient visits");
    expect(sortContributions(ranked, "lowers-first")[0]?.feature).toBe("Lab procedures");
    expect(sortContributions(ranked, "alphabetical").map((item) => item.feature)).toEqual([
      "Age bracket",
      "Inpatient visits",
      "Insulin status: Up",
      "Lab procedures",
      "Number of diagnoses",
    ]);
    expect(sortContributions(ranked, "impact")).toEqual(ranked);
  });

  it("does not mutate its input", () => {
    const before = ranked.map((item) => item.feature);
    sortContributions(ranked, "alphabetical");
    expect(ranked.map((item) => item.feature)).toEqual(before);
  });

  it("filters by direction", () => {
    expect(filterContributions(ranked, "raises").every((item) => item.contribution >= 0)).toBe(true);
    expect(filterContributions(ranked, "lowers")).toHaveLength(2);
    expect(filterContributions(ranked, "all")).toHaveLength(5);
  });
});

describe("formatters", () => {
  it("formats percentages, signed values, and clamps gauge widths", () => {
    expect(formatPercent(0.4123)).toBe("41.2%");
    expect(formatSigned(0.2312)).toBe("+0.231");
    expect(formatSigned(-0.0356, 4)).toBe("−0.0356");
    expect(clampPercent(1.4)).toBe(100);
    expect(clampPercent(-0.2)).toBe(0);
  });
});
