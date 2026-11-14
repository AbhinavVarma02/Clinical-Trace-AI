// The hosted read-only copy: one owner-approved publication document, fetched same-origin with no credentials.
// It is checked as strictly as the genuine owner read; anything else shows nothing.
import type { BetaView } from "./contract";

export const PUBLICATION_FORMAT = "clinical-trace-epic-beta-publication/1";
export const PUBLICATION_FILE = "published/record.json";

export interface Publication {
  format: typeof PUBLICATION_FORMAT;
  exported_at: number;
  withheld: string[];
  view: BetaView;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const ALIAS = /^[a-z0-9][a-z0-9-]{2,63}$/;
const SECTIONS = ["conditions", "encounters", "labs", "vitals", "reports", "notes"] as const;
const LABELS = { epic_sandbox: "Approved LIVE patient", synthetic_live_fixture: "Synthetic LIVE-shaped fixture" } as const;

/** Every synthetic marker must agree with the origin, so flipping one flag cannot relabel a fixture as Epic data. */
function consistent(view: BetaView, fixture: boolean): boolean {
  return view.label === (fixture ? LABELS.synthetic_live_fixture : LABELS.epic_sandbox) &&
    view.warnings.some((warning) => warning.code === "synthetic_live_fixture") === fixture &&
    (view.record?.notes.items ?? []).every((item) => (item as { source?: { synthetic?: unknown } }).source?.synthetic === fixture);
}

/** Accept only a verified LIVE projection labelled as a never-refreshed copy, with the attempt identity withheld.
 *  An invented synthetic fixture is accepted only by a local preview build (VITE_BETA_ALLOW_FIXTURE=true). */
export function checkPublication(value: unknown, allowFixture = import.meta.env.VITE_BETA_ALLOW_FIXTURE === "true"): Publication {
  if (!object(value) || value.format !== PUBLICATION_FORMAT || typeof value.exported_at !== "number" || !Number.isFinite(value.exported_at) ||
      !Array.isArray(value.withheld) || !value.withheld.includes("attempt_id") || !object(value.view)) {
    throw new Error("Published record refused");
  }
  const view = value.view as unknown as BetaView;
  const provenance = view.provenance as unknown as Record<string, unknown> | null;
  const fixture = view.data_origin === "synthetic_live_fixture";
  if (view.mode !== "LIVE" || (view.data_origin !== "epic_sandbox" && !(fixture && allowFixture)) || view.source !== "epic-fhir-sandbox-r4" ||
      view.completeness !== "unconfirmed" || view.publication !== "published_snapshot" || view.serving !== "stale" || view.freshness !== "stale" ||
      view.evidence_error !== null || typeof view.alias !== "string" || !ALIAS.test(view.alias) || !object(view.record) || !object(view.active) ||
      !object(provenance) || "attempt_id" in provenance || provenance.synthetic_transport !== fixture || provenance.mode !== "LIVE" ||
      provenance.source !== view.source || provenance.generation_id !== view.active.generation_id || !/^[a-f0-9]{32}$/.test(view.active.generation_id) ||
      typeof provenance.manifest_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(provenance.manifest_sha256) ||
      typeof provenance.bundle_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(provenance.bundle_sha256) ||
      !object(view.record.patient) || !SECTIONS.every((name) => Array.isArray(view.record?.[name]?.items)) ||
      !Array.isArray(view.warnings) || !view.warnings.some((warning) => warning.code === "published_snapshot") || !consistent(view, fixture)) {
    throw new Error("Published record refused");
  }
  return value as unknown as Publication;
}

export async function loadPublication(signal?: AbortSignal): Promise<Publication> {
  const response = await fetch(import.meta.env.BASE_URL + PUBLICATION_FILE, {
    method: "GET", credentials: "omit", mode: "same-origin", cache: "no-store", redirect: "error",
    referrerPolicy: "no-referrer", headers: { Accept: "application/json" }, signal,
  });
  if (!response.ok) throw new Error("Published record unavailable");
  return checkPublication(await response.json());
}
