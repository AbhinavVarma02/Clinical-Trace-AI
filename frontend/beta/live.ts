// Hosted LIVE Beta client: same-origin calls to the owner-only backend behind Cloudflare Access.
// Every record is checked as strictly as the genuine owner read. Invented fixtures are always refused: this build
// has no fixture path, and a synthetic-transport candidate or view never renders as Epic data.
import type { BetaView } from "./contract";
import type { CandidateBinding, CandidateResult } from "./genuine";

const API = "/beta/api/";
const ALIAS = /^[a-z0-9][a-z0-9-]{2,63}$/;
const HASH = /^[a-f0-9]{64}$/;
const SECTIONS = ["conditions", "encounters", "labs", "vitals", "reports", "notes"] as const;

export class LiveApiError extends Error {
  constructor(readonly status: number, readonly code: string, readonly retryAfter: number | null = null) {
    super(code);
  }
}

export interface LiveSession {
  owner: true;
  mode: "LIVE";
  source: string;
  patient: { alias: string };
  epic_enabled: boolean;
  server_time: number;
  access_expires: number;
}

export interface LiveStatus {
  attempt: { attempt_id: string; state: string; error_code: string | null; requests: number | null;
    operations: Record<string, { http_statuses?: number[]; matches?: number | null; informational_codes?: string[] }> } | null;
  candidate: { attempt_id: string; state: string; manifest_sha256: string | null; expires_at: number } | null;
  generation: { generation_id: string; state: string; freshness: string } | null;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function call(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(API + path, {
    credentials: "same-origin", mode: "same-origin", cache: "no-store", redirect: "manual", referrerPolicy: "no-referrer",
    ...init,
    headers: { Accept: "application/json", "X-Beta-Client": "live", ...(init.headers ?? {}) },
  });
  // An expired Cloudflare Access session answers with a redirect to its sign-in page.
  if (response.type === "opaqueredirect" || response.status === 0) throw new LiveApiError(401, "access_session_expired");
  if (!response.ok) {
    let code = "request_failed";
    try {
      const body: unknown = await response.json();
      if (object(body) && object(body.error) && typeof body.error.code === "string") code = body.error.code;
    } catch { /* not JSON */ }
    const retry = Number(response.headers.get("retry-after"));
    throw new LiveApiError(response.status, code, Number.isFinite(retry) && retry > 0 ? retry : null);
  }
  return response;
}

/** Accept only a verified, provider-acquired LIVE projection served to its owner. */
export function checkLiveView(value: unknown): BetaView {
  const view = value as BetaView;
  const provenance = (object(value) ? view.provenance : null) as unknown as Record<string, unknown> | null;
  const warnings = Array.isArray(view?.warnings) ? view.warnings.map((warning) => warning?.code) : [];
  if (!object(value) || view.mode !== "LIVE" || view.data_origin !== "epic_sandbox" || view.label !== "Approved LIVE patient" ||
      view.source !== "epic-fhir-sandbox-r4" || view.completeness !== "unconfirmed" || view.publication !== "disabled" ||
      !["fresh", "cached", "stale"].includes(view.serving) || view.serving !== view.freshness || view.evidence_error !== null ||
      typeof view.alias !== "string" || !ALIAS.test(view.alias) || !object(view.record) || !object(view.active) || !object(provenance) ||
      provenance.synthetic_transport !== false || provenance.mode !== "LIVE" || provenance.source !== view.source ||
      provenance.generation_id !== view.active.generation_id || !/^[a-f0-9]{32}$/.test(view.active.generation_id) ||
      typeof provenance.manifest_sha256 !== "string" || !HASH.test(provenance.manifest_sha256) ||
      typeof provenance.bundle_sha256 !== "string" || !HASH.test(provenance.bundle_sha256) ||
      !object(view.record.patient) || !SECTIONS.every((name) => Array.isArray(view.record?.[name]?.items)) ||
      !warnings.includes("hosted_owner_only") || warnings.includes("synthetic_live_fixture") ||
      !view.record.notes.items.every((item) => (item as { source?: { synthetic?: unknown } }).source?.synthetic === false)) {
    throw new Error("Live record refused");
  }
  return view;
}

function checkCandidate(value: unknown): CandidateResult {
  const body = value as Record<string, unknown>;
  const candidate = (object(value) ? body.candidate : null) as Record<string, unknown> | null;
  const review = (object(value) ? body.review : null) as Record<string, unknown> | null;
  if (!object(value) || body.synthetic_transport !== false || typeof body.alias !== "string" || !ALIAS.test(body.alias) ||
      typeof body.patient_identifier !== "string" || !/^[A-Za-z0-9.-]{1,64}$/.test(body.patient_identifier) ||
      !object(candidate) || typeof candidate.attempt_id !== "string" || !/^[A-Za-z0-9_-]{16,64}$/.test(candidate.attempt_id) ||
      typeof candidate.generation_id !== "string" || !/^[a-f0-9]{32}$/.test(candidate.generation_id) ||
      typeof candidate.manifest_sha256 !== "string" || !HASH.test(candidate.manifest_sha256) ||
      typeof candidate.bundle_sha256 !== "string" || !HASH.test(candidate.bundle_sha256) ||
      !object(review) || review.mode !== "LIVE" || review.source !== "epic-fhir-sandbox-r4" || review.completeness !== "unconfirmed" ||
      typeof review.retrieved_at !== "number" || !object(review.resource_counts) || !object(review.operations) ||
      !object(review.http_outcomes) || !object(review.source_timestamps) || !object(review.mapping)) {
    throw new Error("Live candidate refused");
  }
  return {
    operation: "acquire",
    alias: body.alias,
    patient_identifier: body.patient_identifier,
    provenance: candidate as unknown as CandidateBinding,
    review: review as unknown as CandidateResult["review"],
  };
}

export async function liveSession(signal?: AbortSignal): Promise<LiveSession> {
  const body: unknown = await (await call("session", { signal })).json();
  if (!object(body) || body.owner !== true || body.mode !== "LIVE" || !object(body.patient) ||
      typeof body.patient.alias !== "string" || !ALIAS.test(body.patient.alias) || typeof body.epic_enabled !== "boolean") {
    throw new Error("Live session refused");
  }
  return body as unknown as LiveSession;
}

/** The approved record, or null when nothing has been retrieved and approved yet. */
export async function liveRecord(signal?: AbortSignal): Promise<BetaView | null> {
  try {
    return checkLiveView(await (await call("record", { signal })).json());
  } catch (error) {
    if (error instanceof LiveApiError && error.status === 404) return null;
    throw error;
  }
}

export async function liveStatus(signal?: AbortSignal): Promise<LiveStatus> {
  return (await (await call("status", { signal })).json()) as LiveStatus;
}

export async function liveRefresh(signal?: AbortSignal): Promise<CandidateResult> {
  return checkCandidate(await (await call("refresh", { method: "POST", signal })).json());
}

export async function liveApprove(binding: CandidateBinding, signal?: AbortSignal): Promise<BetaView> {
  const response = await call("approve", {
    method: "POST", signal, headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ attempt_id: binding.attempt_id, manifest_sha256: binding.manifest_sha256 }),
  });
  return checkLiveView(await response.json());
}

export async function liveErase(signal?: AbortSignal): Promise<void> {
  await call("erase", { method: "POST", signal });
}

const MESSAGES: Record<string, string> = {
  access_session_expired: "Your Cloudflare Access session has ended. Reload the page to sign in again.",
  owner_required: "Owner sign-in is required. Reload the page to sign in again.",
  live_disabled: "Live Epic retrieval is switched off on the hosted backend.",
  quota_exhausted: "The Epic retrieval budget for this patient is used up for now.",
  refresh_busy: "A retrieval is already running.",
  rate_limited: "Too many requests; wait a minute.",
  backend_starting: "The hosted backend is starting. Try again in a few seconds.",
  backend_not_configured: "The hosted backend is not configured yet.",
  candidate_hash_mismatch: "The candidate changed; refresh again before approving.",
  genuine_candidate_required: "Only provider-acquired Epic data can be shown.",
};

export function describe(error: unknown): string {
  if (error instanceof LiveApiError) {
    const base = MESSAGES[error.code] ?? `Request refused (${error.code}, HTTP ${error.status}).`;
    return error.retryAfter ? `${base} Retry in about ${Math.ceil(error.retryAfter / 60)} min.` : base;
  }
  return error instanceof Error ? error.message : "Request failed";
}
