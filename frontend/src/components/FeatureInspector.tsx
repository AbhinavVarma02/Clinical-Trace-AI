import { formatPercent, formatSigned } from "../lib/contributions";
import type { RankedContribution } from "../types";

interface FeatureInspectorProps {
  item: RankedContribution | null;
  total: number;
}

/** Detail panel for the selected contribution. */
export function FeatureInspector({ item, total }: FeatureInspectorProps) {
  if (!item) {
    return (
      <aside className="inspector" aria-label="Feature details">
        <p className="muted">Select a bar to inspect its contribution.</p>
      </aside>
    );
  }
  const raises = item.direction === "raises";
  return (
    <aside className="inspector" aria-label="Feature details" data-testid="inspector">
      <div className="inspector-eyebrow">Selected signal</div>
      <div className="inspector-title">{item.feature}</div>
      <span className={`dir-chip ${item.direction}`}>
        <span aria-hidden="true">{raises ? "▲" : "▼"}</span>
        {raises ? "Raises model risk score" : "Lowers model risk score"}
      </span>
      <dl className="inspector-grid">
        <div>
          <dt>SHAP contribution</dt>
          <dd data-testid="inspector-contribution">{formatSigned(item.contribution, 4)}</dd>
        </div>
        <div>
          <dt>Impact rank</dt>
          <dd>
            #{item.impactRank} of {total}
          </dd>
        </div>
        <div>
          <dt>Share of displayed signal</dt>
          <dd>{formatPercent(item.share)}</dd>
        </div>
        <div>
          <dt>Relative to strongest</dt>
          <dd>{formatPercent(item.relativeToMax, 0)}</dd>
        </div>
      </dl>
      <p className="inspector-note">
        Contributions are in model-score units (not probability points) and describe how this signal moved the
        model's output for this synthetic encounter. They are model-ranked signals, not clinical causes.
      </p>
    </aside>
  );
}
