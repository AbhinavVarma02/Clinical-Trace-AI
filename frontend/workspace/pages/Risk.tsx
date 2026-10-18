import { api } from "../api";
import { ContributionBars, ProbabilityMeter } from "../components/charts";
import { Badge, Card, Disclosure, ErrorState, Skeleton, Stat, pct } from "../components/ui";
import { useApi, useApp } from "../state";
import { PatientList } from "./Patients";

export function RiskPage() {
  const { patientId } = useApp();
  const risk = useApi(() => api.risk(patientId), `risk:${patientId}`);
  const r = risk.data;
  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="page-title">Risk & Explainability</h1>
          <p className="page-subtitle">An XGBoost model trained on public UCI diabetes data. It runs only when a record maps to its inputs.</p>
        </div>
      </div>
      <div className="split">
        <PatientList compact />
        <div className="stack" style={{ gap: "var(--space-4)", minWidth: 0 }}>
          {risk.loading && <Card><Skeleton lines={6} /></Card>}
          {risk.error && <Card><ErrorState error={risk.error} onRetry={risk.reload} /></Card>}
          {r && !r.available && (
            <Card title="Model not applicable to this record" subtitle="No prediction is shown rather than an unsupported one">
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {(r.reasons ?? []).map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            </Card>
          )}
          {r?.available && r.prediction && (
            <>
              <div className="grid cols-3">
                <Stat label="Model probability" value={pct(r.prediction.risk_probability)} tone={r.prediction.readmission_risk === "high" ? "danger" : "success"} note={`Encounter ${r.encounter_id}`} />
                <Stat label="Label at threshold" value={<span style={{ textTransform: "capitalize" }}>{r.prediction.readmission_risk}</span>} note={`threshold ${pct(r.prediction.risk_threshold, 0)}`} />
                <Stat label="Model version" value={<span className="mono" style={{ fontSize: 15 }}>{r.prediction.model_version}</span>} note={r.model?.model_type} />
              </div>
              <Card title="Probability versus decision threshold">
                <ProbabilityMeter probability={r.prediction.risk_probability} threshold={r.prediction.risk_threshold} />
              </Card>
              {r.model?.calibration && <Card title="Calibration and uncertainty" subtitle={r.model.calibration.dataset}>
                <p>Average model score {pct(r.model.calibration.mean_probability)} versus observed readmission rate {pct(r.model.calibration.prevalence)} across {r.model.calibration.rows.toLocaleString()} test encounters.</p>
                <p>Brier score {r.model.calibration.brier.toFixed(3)} · ECE {r.model.calibration.ece.toFixed(3)}.</p>
                <p className="muted">Scores overestimate the observed rate. No recalibration has been applied, and calibration on synthetic patients is unknown. Do not read the score as an individual confidence estimate.</p>
              </Card>}
              <Card title="What drove the score" subtitle="SHAP contribution of each feature">
                <ContributionBars features={r.prediction.top_features} />
              </Card>
              <Card title="Interpretation caveats">
                <ul className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                  {(r.caveats ?? []).map((c) => <li key={c}>{c}</li>)}
                </ul>
                <p className="faint" style={{ fontSize: 12, marginTop: 10 }}>{r.disclaimer}</p>
              </Card>
              <Disclosure label="Model details" hint="Benchmark metrics and mapped inputs">
                <Card title="Benchmark performance" subtitle="Held-out UCI test split (patient-grouped)">
                  {r.model && (
                    <dl className="kv">
                      {Object.entries(r.model.test_metrics).map(([key, value]) => (
                        <div key={key} style={{ display: "contents" }}>
                          <dt>{key.replace(/_/g, " ")}</dt>
                          <dd>{value === null ? "—" : value.toFixed(3)}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                  <p className="faint" style={{ fontSize: 12, marginTop: 10 }}>{r.model?.evaluation_protocol}</p>
                  <p className="faint" style={{ fontSize: 12 }}>These metrics describe the UCI benchmark, not these synthetic records.</p>
                </Card>
                <Card title="Mapped model inputs" subtitle="Derived from the synthetic FHIR record by src/clinical/risk_bridge.py">
                  <div className="row" style={{ gap: 6 }}>
                    {Object.entries(r.features ?? {}).filter(([k]) => k !== "patient_id").map(([key, value]) => (
                      <Badge key={key} tone="outline">
                        <span className="faint">{key}</span>&nbsp;<span className="mono">{String(value)}</span>
                      </Badge>
                    ))}
                  </div>
                </Card>
              </Disclosure>
            </>
          )}
        </div>
      </div>
    </>
  );
}
