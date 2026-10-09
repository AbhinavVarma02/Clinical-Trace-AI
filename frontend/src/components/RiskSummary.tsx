import { clampPercent, formatPercent } from "../lib/contributions";
import type { ReadyExplorerData } from "../types";

interface RiskSummaryProps {
  data: ReadyExplorerData;
}

/** Probability, backend risk label, and decision threshold — all as returned by Python. */
export function RiskSummary({ data }: RiskSummaryProps) {
  const isHigh = data.riskLabel === "high";
  const tone = isHigh ? "red" : "green";
  return (
    <section className="card" aria-label="Risk summary">
      <div className="card-title">Readmission risk estimate</div>
      <div className="risk-grid">
        <div className="risk-stat">
          <span className="stat-key">Predicted probability</span>
          <span className="stat-val" data-testid="probability">
            {formatPercent(data.probability)}
          </span>
        </div>
        <div className="risk-stat">
          <span className="stat-key">Backend risk label</span>
          <span className={`risk-chip ${tone}`} data-testid="risk-label">
            <span aria-hidden="true">{isHigh ? "▲" : "▼"}</span>
            {isHigh ? "High" : "Low"}
          </span>
        </div>
        <div className="risk-stat">
          <span className="stat-key">Decision threshold</span>
          <span className="stat-val" data-testid="threshold">
            {data.threshold.toFixed(2)}
          </span>
        </div>
      </div>
      <div
        className="gauge"
        role="img"
        aria-label={`Probability ${formatPercent(data.probability)} versus decision threshold ${data.threshold.toFixed(2)}`}
      >
        <div className="gauge-fill" style={{ width: `${clampPercent(data.probability).toFixed(1)}%` }} />
        <div className="gauge-marker" style={{ left: `${clampPercent(data.threshold).toFixed(1)}%` }} />
      </div>
      <div className="gauge-legend">
        <span className="legend-line" aria-hidden="true" /> Decision threshold marker
        <span className="gauge-note">
          {isHigh ? "At or above the threshold" : "Below the threshold"}
        </span>
      </div>
      <div className="pill-row">
        <span className="pill">{data.patientId}</span>
        {data.displayRiskLabel && <span className="pill">Dashboard tier: {data.displayRiskLabel}</span>}
        <span className="pill">Model {data.modelVersion}</span>
      </div>
    </section>
  );
}
