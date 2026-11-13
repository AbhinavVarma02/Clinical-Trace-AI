// Beta client seam with two implementations of one interface.
//   fixtureClient: in-memory synthetic fixtures; performs no network request (unit tests, design review).
//   httpClient:    same-origin reads of the Beta browser contract (`/beta/epic/patients` and
//                  `/beta/epic/patients/{alias}/record`; a 401 means sign in), plus the owner actions: refresh, ask a
//                  question of the active MOCK generation, revoke a patient, sign out. It reads no cookie or storage and
//                  keeps no token: the CSRF token is fetched for each owner action and dropped. Sign-in is a plain form
//                  POST handled by the page, not by this client.
import { PATIENT_CHOICES, VIEWS } from "./fixtures";
import type { Answer, BetaView, PatientChoice } from "./contract";

export interface BetaClient {
  readonly kind: "fixture" | "http";
  listPatients(): Promise<PatientChoice[]>;
  getView(alias: string): Promise<BetaView>;
  /** Passive session check. Only a fingerprint and conservative local expiry survive the response. */
  getSession?(): Promise<{ binding: string; expiresAt: number }>;
  /** Start an owner refresh of one MOCK patient (202 = accepted). Absent on the fixture client. */
  refresh?(alias: string): Promise<void>;
  /** Ask one question of one pinned generation. `acknowledgeCache` is required when it is not fresh. */
  ask?(alias: string, generationId: string, question: string, acknowledgeCache: boolean): Promise<Answer>;
  /** Revoke one MOCK patient for this owner store. Irreversible from the browser. */
  revoke?(alias: string): Promise<void>;
  /** End the owner session. */
  signOut?(): Promise<void>;
}

export class UnknownPatientError extends Error {
  constructor() {
    super("patient_unavailable");
  }
}

/** A refused or failed request. `code` is the server's content-free error code; `status` is the HTTP status. */
export class BetaApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfter: number | null = null,
  ) {
    super(code);
  }
}

function lookup<T>(table: Record<string, T>, alias: string): T {
  // Own-property check: aliases come from UI state and must never reach prototype members.
  if (!Object.prototype.hasOwnProperty.call(table, alias)) throw new UnknownPatientError();
  return structuredClone(table[alias] as T);
}

export const fixtureClient: BetaClient = {
  kind: "fixture",
  listPatients: async () => structuredClone(PATIENT_CHOICES),
  getView: async (alias) => lookup(VIEWS, alias),
};

const ALIAS = /^[a-z0-9-]{1,32}$/;
const GENERATION = /^[a-f0-9]{32}$/;
const CODE = /^[a-z0-9_]{1,64}$/;
export const MAX_QUESTION = 1000;

function retryAfter(response: Response): number | null {
  const value = Number(response.headers.get("Retry-After"));
  return Number.isInteger(value) && value > 0 && value <= 86400 ? value : null;
}

async function failure(response: Response): Promise<BetaApiError> {
  let code = "unavailable";
  try {
    const body = (await response.json()) as { error?: { code?: unknown } };
    if (typeof body.error?.code === "string" && CODE.test(body.error.code)) code = body.error.code;
  } catch {
    // keep the generic code; an error body is never shown
  }
  return new BetaApiError(response.status, code, retryAfter(response));
}

async function read<T>(path: string): Promise<T> {
  // These options produce the request profile the server requires: GET, same-origin mode, no body, JSON.
  const response = await fetch(path, {
    method: "GET",
    mode: "same-origin",
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    referrerPolicy: "no-referrer",
    headers: { "X-Beta-Client": "browser-v1", Accept: "application/json" },
  });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

/** One owner action: fetch the CSRF token, then one POST. No request-level `mode` or `referrerPolicy`, so the page's
 * same-origin policy lets the browser send its real Origin. Returns the response when its status is `expected`. */
async function owner(path: string, expected: number, extra: { headers?: Record<string, string>; body?: string } = {}): Promise<Response> {
  const { csrf_token } = await read<{ csrf_token: string }>("/beta/auth/session");
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    headers: { "X-CSRF-Token": csrf_token, Accept: "application/json", ...extra.headers },
    ...(extra.body === undefined ? {} : { body: extra.body }),
  });
  if (response.status !== expected) throw await failure(response);
  return response;
}

function checked(alias: string): string {
  if (!ALIAS.test(alias)) throw new UnknownPatientError();
  return alias;
}

export const httpClient: BetaClient = {
  kind: "http",
  getSession: async () => {
    const requestedAt = Date.now();
    const session = await read<{ authenticated: boolean; mode: string; csrf_token: string; server_time_unix_s: number; idle_expires_at_unix_s: number; absolute_expires_at_unix_s: number }>("/beta/auth/session");
    const remaining = Math.min(session.idle_expires_at_unix_s, session.absolute_expires_at_unix_s) - session.server_time_unix_s;
    if (session.authenticated !== true || session.mode !== "MOCK" || !/^[A-Za-z0-9_-]{43}$/.test(session.csrf_token) || ![session.server_time_unix_s, session.idle_expires_at_unix_s, session.absolute_expires_at_unix_s].every((value) => typeof value === "number" && Number.isFinite(value)) || !Number.isFinite(remaining) || remaining <= 0) {
      throw new BetaApiError(401, "owner_required");
    }
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(session.csrf_token));
    return { binding: Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""), expiresAt: requestedAt + remaining * 1000 };
  },
  listPatients: async () => (await read<{ patients: PatientChoice[] }>("/beta/epic/patients")).patients,
  getView: async (alias) => {
    const view = await read<BetaView>(`/beta/epic/patients/${checked(alias)}/record`);
    // Bind the response to this selection and generation. LIVE is a synthetic preview only; no genuine
    // evidence can become displayable merely by returning the same field shape from this offline app.
    if (view.alias !== alias || !["MOCK", "LIVE"].includes(view.mode) ||
        (view.provenance && (view.provenance.mode !== view.mode || view.provenance.source !== view.source ||
          view.provenance.generation_id !== view.active?.generation_id)) ||
        (view.record && (!view.active || !view.provenance || view.evidence_error)) ||
        (view.mode === "MOCK" && !["fixture", "synthetic_mock_transport"].includes(view.data_origin)) ||
        (view.mode === "LIVE" && (view.data_origin !== "synthetic_live_fixture" ||
          view.serving !== "unavailable" || view.publication !== "disabled" || view.completeness !== "unconfirmed" ||
          view.provenance?.mode !== "LIVE" || view.provenance.source !== view.source ||
          view.provenance.synthetic_transport !== true || !view.record || !view.active))) {
      throw new BetaApiError(409, "record_mismatch");
    }
    return view;
  },
  refresh: async (alias) => {
    await owner(`/beta/epic/patients/${checked(alias)}/refresh`, 202, { headers: { "Idempotency-Key": crypto.randomUUID() } });
  },
  ask: async (alias, generationId, question, acknowledgeCache) => {
    if (!GENERATION.test(generationId)) throw new BetaApiError(404, "generation_unavailable");
    if (question.length < 1 || question.length > MAX_QUESTION) throw new BetaApiError(400, "invalid_question");
    const response = await owner(`/beta/epic/patients/${checked(alias)}/generations/${generationId}/runs`, 200, {
      headers: { "Content-Type": "application/json", ...(acknowledgeCache ? { "X-Beta-Cache-Ack": "1" } : {}) },
      body: JSON.stringify({ question }),
    });
    return (await response.json()) as Answer;
  },
  revoke: async (alias) => {
    await owner(`/beta/epic/patients/${checked(alias)}/revoke`, 204);
  },
  signOut: async () => {
    await owner("/beta/auth/logout", 204);
  },
};
