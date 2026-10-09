import { formatSigned } from "../lib/contributions";
import type { RankedContribution } from "../types";

interface ContributionChartProps {
  items: readonly RankedContribution[];
  selectedFeature: string | null;
  onSelect: (feature: string) => void;
}

/**
 * Diverging horizontal bar chart centred on zero: bars to the right raise the
 * model score, bars to the left lower it. Bar length is relative to the
 * strongest displayed signal, so the scale stays stable while filtering.
 */
export function ContributionChart({ items, selectedFeature, onSelect }: ContributionChartProps) {
  if (items.length === 0) {
    return <p className="muted">No contributions match this filter.</p>;
  }
  return (
    <div className="chart" role="list" aria-label="SHAP feature contributions">
      <div className="chart-axis" aria-hidden="true">
        <span>Lowers risk</span>
        <span>0</span>
        <span>Raises risk</span>
      </div>
      {items.map((item) => {
        const selected = item.feature === selectedFeature;
        const width = `${Math.max(item.relativeToMax * 50, 1.5).toFixed(2)}%`;
        return (
          <div role="listitem" key={item.feature}>
            <button
              type="button"
              className={selected ? "bar-row selected" : "bar-row"}
              aria-pressed={selected}
              aria-label={`${item.feature}: ${item.direction === "raises" ? "raises" : "lowers"} risk, contribution ${formatSigned(item.contribution)}`}
              onClick={() => onSelect(item.feature)}
            >
              <span className="bar-name">{item.feature}</span>
              <span className="bar-area">
                <span className="bar-zero" aria-hidden="true" />
                <span
                  className={`bar ${item.direction}`}
                  data-testid="contribution-bar"
                  style={item.direction === "raises" ? { left: "50%", width } : { right: "50%", width }}
                />
              </span>
              <span className={`bar-value ${item.direction}`}>{formatSigned(item.contribution)}</span>
            </button>
          </div>
        );
      })}
    </div>
  );
}
