// Small, dependency-free SVG charts. Each chart has a text alternative.
import type { FeatureContribution, LabSeries } from "../types";

export function LabTrendChart({ series, height = 150 }: { series: LabSeries; height?: number }) {
  const width = 420;
  const pad = { top: 18, right: 16, bottom: 26, left: 40 };
  const values = series.points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || Math.abs(max) || 1;
  const lo = min - span * 0.2;
  const hi = max + span * 0.2;
  const times = series.points.map((p) => new Date(p.effective).getTime());
  const t0 = Math.min(...times);
  const t1 = Math.max(...times);
  const x = (t: number) => pad.left + (t1 === t0 ? (width - pad.left - pad.right) / 2 : ((t - t0) / (t1 - t0)) * (width - pad.left - pad.right));
  const y = (v: number) => pad.top + (1 - (v - lo) / (hi - lo)) * (height - pad.top - pad.bottom);
  const path = series.points.map((p, i) => `${i === 0 ? "M" : "L"}${x(times[i] ?? 0).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const ticks = [lo + (hi - lo) * 0.15, (lo + hi) / 2, hi - (hi - lo) * 0.15];
  const description = `${series.name}: ${series.points.map((p) => `${p.value} ${p.unit ?? ""} on ${p.effective.slice(0, 10)}`).join("; ")}`;
  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={description}>
      {ticks.map((tick) => (
        <g key={tick}>
          <line className="grid-line" x1={pad.left} x2={width - pad.right} y1={y(tick)} y2={y(tick)} />
          <text x={pad.left - 8} y={y(tick) + 4} textAnchor="end">
            {Number(tick.toPrecision(3))}
          </text>
        </g>
      ))}
      <path className="series" d={path} />
      {series.points.map((p, i) => (
        <g key={p.source.reference}>
          <circle className="point" cx={x(times[i] ?? 0)} cy={y(p.value)} r={4} />
          <text className="value-label" x={x(times[i] ?? 0)} y={y(p.value) - 9} textAnchor="middle">
            {p.value}
          </text>
          <text x={x(times[i] ?? 0)} y={height - 7} textAnchor="middle">
            {new Date(p.effective).toLocaleDateString(undefined, { month: "short", year: "2-digit", timeZone: "UTC" })}
          </text>
        </g>
      ))}
    </svg>
  );
}

export function ContributionBars({ features }: { features: FeatureContribution[] }) {
  const maxAbs = Math.max(...features.map((f) => Math.abs(f.contribution)), 1e-9);
  return (
    <div role="list" aria-label="SHAP feature contributions">
      {features.map((f) => {
        const width = (Math.abs(f.contribution) / maxAbs) * 50;
        const raise = f.contribution > 0;
        return (
          <div className="bar-row" role="listitem" key={f.feature}>
            <span title={f.feature} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {f.feature}
            </span>
            <div className="bar-track" aria-hidden="true">
              <span className="bar-axis" />
              <span className={`bar ${raise ? "raise" : "lower"}`} style={{ width: `${width}%` }} />
            </div>
            <span className="mono" style={{ textAlign: "right" }}>
              {raise ? "+" : ""}
              {f.contribution.toFixed(3)}
            </span>
          </div>
        );
      })}
      <div className="row faint" style={{ justifyContent: "space-between", fontSize: 12, marginTop: 6 }}>
        <span>← lowers model score</span>
        <span>raises model score →</span>
      </div>
    </div>
  );
}

export function ProbabilityMeter({ probability, threshold }: { probability: number; threshold: number }) {
  const high = probability >= threshold;
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="meter" role="img" aria-label={`Probability ${(probability * 100).toFixed(1)} percent; threshold ${(threshold * 100).toFixed(0)} percent`}>
        <div className={`meter-fill ${high ? "high" : ""}`} style={{ width: `${Math.min(probability, 1) * 100}%` }} />
        <div className="meter-marker" style={{ left: `calc(${threshold * 100}% - 1px)` }} title="Decision threshold" />
      </div>
      <div className="row faint" style={{ justifyContent: "space-between", fontSize: 12 }}>
        <span>0%</span>
        <span>threshold {(threshold * 100).toFixed(0)}%</span>
        <span>100%</span>
      </div>
    </div>
  );
}
