/**
 * Data contract between the Streamlit/Python layer and the explorer.
 *
 * Python builds this object from the prediction already stored in
 * ``st.session_state.last_result`` (see dashboard/components/shap_explorer.py);
 * the component never runs inference or recomputes SHAP values.
 */

/** One SHAP contribution exactly as returned in ``prediction.top_features``. */
export interface FeatureContribution {
  feature: string;
  contribution: number;
}

/** Backend label from ``src.predict``: probability >= threshold -> "high". */
export type BackendRiskLabel = "high" | "low";

/** Three-tier label the dashboard already derives for display. */
export type DisplayRiskTier = "High" | "Moderate" | "Low";

export interface ReadyExplorerData {
  status: "ready";
  patientId: string;
  probability: number;
  threshold: number;
  riskLabel: BackendRiskLabel;
  displayRiskLabel: DisplayRiskTier | null;
  modelVersion: string;
  requestId: string;
  features: FeatureContribution[];
  disclaimer: string;
}

export interface EmptyExplorerData {
  status: "empty";
  disclaimer: string;
  message?: string;
}

export type ExplorerData = ReadyExplorerData | EmptyExplorerData;

export type Direction = "raises" | "lowers";

/** A contribution enriched with presentation-only derived fields. */
export interface RankedContribution extends FeatureContribution {
  /** 1-based rank by absolute contribution (1 = strongest signal). */
  impactRank: number;
  magnitude: number;
  direction: Direction;
  /** |contribution| / sum(|contribution|) over the displayed features. */
  share: number;
  /** |contribution| / max(|contribution|) over the displayed features. */
  relativeToMax: number;
}

export type SortMode = "impact" | "raises-first" | "lowers-first" | "alphabetical";

export type DirectionFilter = "all" | Direction;
