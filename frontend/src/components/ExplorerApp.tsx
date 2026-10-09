import { useEffect, useMemo, useState } from "react";

import { filterContributions, rankContributions, sortContributions } from "../lib/contributions";
import type { DirectionFilter, ExplorerData, FeatureContribution, SortMode } from "../types";
import { ContributionChart } from "./ContributionChart";
import { Disclaimer } from "./Disclaimer";
import { EmptyState } from "./EmptyState";
import { FeatureInspector } from "./FeatureInspector";
import { RiskSummary } from "./RiskSummary";
import { SegmentedControl, type SegmentOption } from "./SegmentedControl";

const SORT_OPTIONS: readonly SegmentOption<SortMode>[] = [
  { value: "impact", label: "Impact" },
  { value: "raises-first", label: "Raises first" },
  { value: "lowers-first", label: "Lowers first" },
  { value: "alphabetical", label: "A–Z" },
];

const FILTER_OPTIONS: readonly SegmentOption<DirectionFilter>[] = [
  { value: "all", label: "All" },
  { value: "raises", label: "Raises risk" },
  { value: "lowers", label: "Lowers risk" },
];

const NO_FEATURES: readonly FeatureContribution[] = [];

interface ExplorerAppProps {
  data: ExplorerData;
}

/** Risk & SHAP Explainability Explorer. */
export function ExplorerApp({ data }: ExplorerAppProps) {
  const [sortMode, setSortMode] = useState<SortMode>("impact");
  const [filter, setFilter] = useState<DirectionFilter>("all");
  const [selectedFeature, setSelectedFeature] = useState<string | null>(null);

  const features = data.status === "ready" ? data.features : NO_FEATURES;
  const ranked = useMemo(() => rankContributions(features), [features]);
  const visible = useMemo(
    () => sortContributions(filterContributions(ranked, filter), sortMode),
    [ranked, filter, sortMode],
  );

  // A new prediction arrives as new args: default the inspector to the
  // strongest signal unless the current selection still exists.
  useEffect(() => {
    setSelectedFeature((current) =>
      current && ranked.some((item) => item.feature === current) ? current : (ranked[0]?.feature ?? null),
    );
  }, [ranked]);

  if (data.status === "empty") {
    return (
      <main className="explorer">
        <EmptyState
          title="Awaiting prediction"
          message={data.message || "Run the risk simulator to explore the probability and SHAP contributions."}
        />
        <Disclaimer text={data.disclaimer} />
      </main>
    );
  }

  const selected = ranked.find((item) => item.feature === selectedFeature) ?? null;

  return (
    <main className="explorer">
      <RiskSummary data={data} />
      <section className="card" aria-label="SHAP contribution explorer">
        <div className="card-head">
          <div className="card-title">SHAP contribution explorer</div>
          <span className="muted small">{ranked.length} model signals</span>
        </div>
        {ranked.length === 0 ? (
          <p className="muted" data-testid="no-features">
            No feature contributions were returned for this prediction.
          </p>
        ) : (
          <>
            <div className="controls">
              <SegmentedControl label="Sort" options={SORT_OPTIONS} value={sortMode} onChange={setSortMode} />
              <SegmentedControl label="Show" options={FILTER_OPTIONS} value={filter} onChange={setFilter} />
            </div>
            <div className="explorer-body">
              <ContributionChart items={visible} selectedFeature={selectedFeature} onSelect={setSelectedFeature} />
              <FeatureInspector item={selected} total={ranked.length} />
            </div>
          </>
        )}
      </section>
      <Disclaimer text={data.disclaimer} />
    </main>
  );
}
