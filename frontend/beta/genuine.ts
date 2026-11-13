// Genuine-only manual reads. No bearer, cookie, storage, polling, background Hello or IdP fallback.
import type { BetaView, OperationCounts } from "./contract";
import { OWNER_ORIGIN, genuineOwnerCredential } from "./webauthn";

const BASE = "/owner/genuine/";
type Options = { flow_id: string; publicKey: unknown };
export type PendingEnrollment = { pending_id: string; fingerprint: string; binding: string };
export type OwnerAction = "acquire" | "review" | "promote";
export type CandidateBinding = {
  attempt_id: string;
  generation_id: string;
  manifest_sha256: string;
  bundle_sha256: string;
};
export interface CandidateResult {
  operation: OwnerAction;
  alias: string;
  patient_identifier: string;
  provenance: CandidateBinding;
  review: {
    source: string;
    policy: string;
    completeness: "unconfirmed";
    retrieved_at: number;
    resource_counts: Record<string, number>;
    unresolved_references: number;
    note_bodies: string;
    patient_display_name: string | null;
    document_metadata_count: number;
    mapping: {
      mapping_version: string;
      received: Record<string, number>;
      mapped: Record<string, number>;
      rejected: Record<string, number>;
      field_status: Record<string, number>;
      skipped_types: Record<string, string>;
      dangling_references: number;
    };
    http_outcomes: Record<string, number[]>;
    source_timestamps: Record<string, { count: number; oldest: string; newest: string }>;
    operations: Record<string, Partial<OperationCounts>>;
  };
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAction(value: unknown): value is OwnerAction {
  return value === "acquire" || value === "review" || value === "promote";
}

const ALIAS = /^[a-z0-9][a-z0-9-]{2,63}$/;

async function request(action: string, body: unknown, signal?: AbortSignal): Promise<unknown> {
  if (!window.isSecureContext || window.location.origin !== OWNER_ORIGIN) throw new Error("Trusted owner HTTPS is required");
  if (signal?.aborted || document.visibilityState !== "visible" || !document.hasFocus()) throw new Error("Focused owner window is required");
  const controller = new AbortController();
  const pageWindow = window;
  const pageDocument = document;
  const cancel = () => controller.abort();
  // Arm around transport only. Native Hello owns focus during its ceremony, between these requests.
  signal?.addEventListener("abort", cancel, { once: true });
  pageWindow.addEventListener("blur", cancel);
  pageWindow.addEventListener("focus", cancel);
  pageDocument.addEventListener("visibilitychange", cancel);
  try {
    const response = await fetch(BASE + action, {
      method: "POST", credentials: "omit", mode: "same-origin", cache: "no-store", redirect: "error",
      referrerPolicy: "same-origin", headers: { "X-Owner-Client": "genuine-webauthn-v1", Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: controller.signal,
    });
    if (!response.ok) throw new Error("Owner request refused; approval or a fresh proof is required");
    const value: unknown = await response.json();
    if (signal?.aborted || controller.signal.aborted) throw new Error("Owner action cancelled");
    return value;
  } finally {
    signal?.removeEventListener("abort", cancel);
    pageWindow.removeEventListener("blur", cancel);
    pageWindow.removeEventListener("focus", cancel);
    pageDocument.removeEventListener("visibilitychange", cancel);
  }
}

export async function pendingOwnerEnrollment(signal?: AbortSignal): Promise<PendingEnrollment> {
  const binding = Array.from(crypto.getRandomValues(new Uint8Array(32)), (byte) => String.fromCharCode(byte)).join("");
  const token = btoa(binding).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
  const pending = await request("enroll/pending", { binding: token }, signal) as Omit<PendingEnrollment, "binding">;
  if (!/^[A-Za-z0-9_-]{43}$/.test(pending.pending_id) || !/^[a-f0-9]{64}$/.test(pending.fingerprint)) throw new Error("Enrollment refused");
  return { ...pending, binding: token };
}

export async function completeOwnerEnrollment(pending: PendingEnrollment, signal?: AbortSignal): Promise<void> {
  const context = { pending_id: pending.pending_id, binding: pending.binding };
  const options = await request("enroll/options", context, signal) as Options;
  const credential = await genuineOwnerCredential(options.publicKey, true, signal);
  await request("enroll/finish", { ...context, flow_id: options.flow_id, credential }, signal);
}

async function ownerProof(operation: "verify" | "read" | OwnerAction, context: CandidateBinding | Record<string, never>, signal?: AbortSignal) {
  const options = await request(operation + "/options", context, signal) as Options;
  const credential = await genuineOwnerCredential(options.publicKey, false, signal);
  if (document.visibilityState !== "visible" || !document.hasFocus()) throw new Error("Focused owner window is required");
  const requestedAt = Date.now();
  const payload = await request(operation + "/finish", { flow_id: options.flow_id, credential }, signal);
  if (!object(payload) || !object(payload.result) || typeof payload.server_time !== "number" || typeof payload.display_until !== "number" || !Number.isFinite(payload.server_time) || !Number.isFinite(payload.display_until)) {
    throw new Error("Owner result refused");
  }
  const remaining = payload.display_until - payload.server_time;
  const budget = Math.min(30, remaining) * 1000;
  // Acquisition may exceed the short view window. Its receipt expires at the same-host epoch
  // deadline set after processing, with the same upper bound on how long the browser displays it.
  const expiresAt = operation === "acquire"
    ? Math.min(payload.display_until * 1000, Date.now() + budget)
    : requestedAt + budget;
  if (document.visibilityState !== "visible" || !document.hasFocus() || remaining <= 0 || expiresAt <= Date.now()) throw new Error("Owner proof expired or window lost focus");
  return { result: payload.result, expiresAt };
}

export async function genuineOwnerRead(operation: "verify" | "read", signal?: AbortSignal): Promise<{ result: BetaView | { authenticated: true }; expiresAt: number }> {
  const { result, expiresAt } = await ownerProof(operation, {}, signal);
  if (operation === "verify") {
    if (result.authenticated !== true) throw new Error("Owner verification refused");
    return { result: { authenticated: true }, expiresAt };
  }
  const view = result as unknown as BetaView;
  if (view.mode !== "LIVE" || view.data_origin !== "epic_sandbox" || view.source !== "epic-fhir-sandbox-r4" ||
        view.completeness !== "unconfirmed" || view.publication !== "disabled" || !["fresh", "cached", "stale"].includes(view.serving) || view.serving !== view.freshness ||
        !view.record || !view.active || !view.provenance || view.evidence_error || !textMatches(view.alias, ALIAS) ||
        view.provenance.synthetic_transport !== false || view.provenance.mode !== "LIVE" || view.provenance.source !== view.source ||
        view.provenance.generation_id !== view.active.generation_id || !/^[a-f0-9]{32}$/.test(view.active.generation_id) ||
        !/^[a-f0-9]{64}$/.test(view.provenance.manifest_sha256 ?? "") || !/^[a-f0-9]{64}$/.test(view.provenance.bundle_sha256 ?? "")) throw new Error("Genuine record refused");
  return { result: view, expiresAt };
}

export async function genuineOwnerActions(signal?: AbortSignal): Promise<OwnerAction[]> {
  const payload = await request("operations", {}, signal);
  if (!object(payload) || !Array.isArray(payload.actions) || !payload.actions.every(isAction)) throw new Error("Owner actions refused");
  const actions: OwnerAction[] = payload.actions;
  if (new Set(actions).size !== actions.length || (actions.includes("acquire") && actions.includes("promote"))) throw new Error("Owner actions refused");
  return actions;
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function counts(value: unknown): boolean {
  return object(value) && Object.values(value).every(isCount);
}

function candidateMetadata(review: Record<string, unknown>): boolean {
  const { mapping, http_outcomes, source_timestamps } = review;
  return (review.patient_display_name === null || typeof review.patient_display_name === "string") &&
    isCount(review.document_metadata_count) && object(mapping) && typeof mapping.mapping_version === "string" &&
    [mapping.received, mapping.mapped, mapping.rejected, mapping.field_status].every(counts) &&
    isCount(mapping.dangling_references) && object(mapping.skipped_types) &&
    Object.values(mapping.skipped_types).every((value) => typeof value === "string") &&
    object(http_outcomes) && Object.values(http_outcomes).every((statuses) => Array.isArray(statuses) &&
      statuses.every((status: unknown) => isCount(status) && status >= 100 && status <= 599)) &&
    object(source_timestamps) && Object.values(source_timestamps).every((range) => object(range) &&
      isCount(range.count) && typeof range.oldest === "string" && typeof range.newest === "string");
}

function textMatches(value: unknown, pattern: RegExp): value is string {
  return typeof value === "string" && pattern.test(value);
}

export async function genuineOwnerOperation(operation: OwnerAction, reviewed: CandidateBinding | null, signal?: AbortSignal): Promise<{ result: CandidateResult; expiresAt: number }> {
  if (operation === "promote" && !reviewed) throw new Error("Review the exact candidate before promotion");
  const { result, expiresAt } = await ownerProof(operation, operation === "promote" ? reviewed! : {}, signal);
  const { provenance, review } = result;
  if (result.operation !== operation || result.synthetic_transport !== false || result.live_serving !== "disabled" || result.clinical_ai !== "disabled" ||
      !textMatches(result.alias, ALIAS) || !textMatches(result.patient_identifier, /^[A-Za-z0-9.-]{1,64}$/) ||
      !object(provenance) || Object.keys(provenance).length !== 4 || !textMatches(provenance.attempt_id, /^[A-Za-z0-9_-]{16,64}$/) || !textMatches(provenance.generation_id, /^[a-f0-9]{32}$/) ||
      !textMatches(provenance.manifest_sha256, /^[a-f0-9]{64}$/) || !textMatches(provenance.bundle_sha256, /^[a-f0-9]{64}$/) ||
      !object(review) || review.mode !== "LIVE" || review.source !== "epic-fhir-sandbox-r4" || review.completeness !== "unconfirmed" ||
      typeof review.policy !== "string" || typeof review.note_bodies !== "string" || typeof review.retrieved_at !== "number" || !Number.isFinite(new Date(review.retrieved_at * 1000).getTime()) ||
      !isCount(review.unresolved_references) || !counts(review.resource_counts) || !candidateMetadata(review) ||
      !object(review.operations) || !Object.values(review.operations).every((item) => object(item) &&
        [item.execution, item.import].every((value) => value == null || typeof value === "string") &&
        [item.matches, item.imported].every((value) => value == null || isCount(value)) &&
        (item.informational_codes === undefined || (Array.isArray(item.informational_codes) && item.informational_codes.every((value: unknown) => typeof value === "string"))))) {
    throw new Error("Genuine candidate refused");
  }
  if (operation === "promote" && reviewed && Object.keys(reviewed).some((key) => provenance[key] !== reviewed[key as keyof CandidateBinding])) {
    throw new Error("Genuine candidate binding changed");
  }
  return { result: result as unknown as CandidateResult, expiresAt };
}
