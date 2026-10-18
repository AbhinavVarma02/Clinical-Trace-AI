// Thin typed client for /api/v2. Every request carries the selected demo persona.
import type {
  AgentRun,
  ApiErrorBody,
  AuditEvent,
  DocumentDetail,
  EvaluationReport,
  OpsMetrics,
  PatientDetail,
  PatientListItem,
  Persona,
  PolicyMatrix,
  RecentRun,
  RetrievalResponse,
  Review,
  RiskResponse,
  SystemStatus,
  TimelineEvent,
} from "./types";

export class ApiError extends Error {
  readonly receivedAt = Date.now();
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
    public limit?: { scope?: "visitor" | "deployment"; kind?: "agent_runs" | "api_requests"; retryAfterS?: number; resetsAt?: string },
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function limitResetAt(error: ApiError): number {
  if (error.code === "daily_limit") {
    const reset = error.limit?.resetsAt ? Date.parse(error.limit.resetsAt) : NaN;
    if (Number.isFinite(reset)) return reset;
    const d = new Date(error.receivedAt);
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
  }
  return error.receivedAt + (error.limit?.retryAfterS ?? 60) * 1000;
}

let currentPersona = "clinician.demo";

export function setApiPersona(persona: string): void {
  currentPersona = persona;
}

/** Errors meaning "the backend is starting or briefly unreachable", which recover on their own. */
export const TRANSIENT_CODES = new Set(["backend_waking", "backend_unavailable", "backend_timeout", "network_error"]);

export function isTransient(error: ApiError | null | undefined): boolean {
  return Boolean(error && TRANSIENT_CODES.has(error.code));
}

let transientListener: ((code: string) => void) | null = null;
let limitListener: ((error: ApiError) => void) | null = null;

export function setLimitListener(listener: ((error: ApiError) => void) | null): void {
  limitListener = listener;
}

function responseError(response: Response, body: ApiErrorBody, fallback: string): ApiError {
  const e = body.error;
  const retry = e?.retry_after_s ?? Number(response.headers.get("retry-after"));
  return new ApiError(response.status, e?.code ?? `http_${response.status}`, e?.message ?? fallback, e?.request_id,
    { scope: e?.scope, kind: e?.limit_kind, retryAfterS: Number.isFinite(retry) && retry > 0 ? retry : undefined, resetsAt: e?.resets_at });
}

/** The availability monitor subscribes so a failed request immediately speeds up status polling. */
export function setTransientListener(listener: ((code: string) => void) | null): void {
  transientListener = listener;
}

function newWorkspaceId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

let workspaceId: string | null = null;

/**
 * Opaque, random per-browser workspace. Public deployments keep each visitor's runs, reviews and
 * audit trail separate by this value; it is not an account and carries no personal data.
 */
export function getWorkspaceId(): string {
  if (workspaceId) return workspaceId;
  try {
    const stored = window.localStorage.getItem("ct-workspace");
    if (stored && /^[a-f0-9]{32}$/.test(stored)) workspaceId = stored;
  } catch {
    // Storage unavailable: the workspace then lasts for this page load only.
  }
  if (!workspaceId) {
    workspaceId = newWorkspaceId();
    try {
      window.localStorage.setItem("ct-workspace", workspaceId);
    } catch {
      // Ignore; see above.
    }
  }
  return workspaceId;
}

/** Start a fresh private workspace (previous runs and reviews stay on the server but become unreachable). */
export function resetWorkspaceId(): string {
  workspaceId = newWorkspaceId();
  try {
    window.localStorage.setItem("ct-workspace", workspaceId);
  } catch {
    // Ignore.
  }
  return workspaceId;
}

function baseHeaders(init?: HeadersInit): Headers {
  const headers = new Headers(init);
  headers.set("X-Demo-Persona", currentPersona);
  headers.set("X-Demo-Workspace", getWorkspaceId());
  return headers;
}

function failure(error: ApiError): ApiError {
  if (TRANSIENT_CODES.has(error.code)) transientListener?.(error.code);
  if (error.status === 429 && (error.code === "daily_limit" || error.code === "rate_limited")) limitListener?.(error);
  return error;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = baseHeaders(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`/api/v2${path}`, { ...init, headers });
  } catch {
    throw failure(new ApiError(0, "network_error", "The API could not be reached. Check your connection; the workspace retries automatically."));
  }
  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // Non-JSON error body; fall through to the generic message.
    }
    const message =
      body.error?.message ??
      (response.status === 422 ? "The request was not valid. Check the input and try again." : `Request failed (HTTP ${response.status}).`);
    throw failure(responseError(response, body, message));
  }
  return (await response.json()) as T;
}

const post = <T>(path: string, body: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body) });

export interface AgentProgress {
  run_id: string;
  node: string;
  duration_ms: number;
  tool?: string;
  ok?: boolean;
}

async function runAgentStream(
  body: { patient_id: string; question: string; session_id?: string | null },
  onProgress: (event: AgentProgress) => void,
): Promise<AgentRun> {
  const headers = baseHeaders({ "Content-Type": "application/json" });
  const response = await fetch("/api/v2/agent/runs/stream", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  }).catch(() => { throw failure(new ApiError(0, "network_error", "The API could not be reached. Check your connection and try again.")); });
  if (!response.ok) {
    let error: ApiErrorBody = {};
    try {
      error = await response.json() as ApiErrorBody;
    } catch {
      // Non-JSON error body.
    }
    throw failure(responseError(response, error, "Execution could not start."));
  }
  if (!response.body) throw new ApiError(0, "stream_unavailable", "Streaming is unavailable in this browser.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const lines = buffer.slice(0, boundary).split("\n");
        buffer = buffer.slice(boundary + 2);
        const kind = lines.find(line => line.startsWith("event: "))?.slice(7);
        const data = lines.filter(line => line.startsWith("data: ")).map(line => line.slice(6)).join("\n");
        if (!kind || !data) continue;
        if (kind === "result") return JSON.parse(data) as AgentRun;
        if (kind === "progress") onProgress(JSON.parse(data) as AgentProgress);
        if (kind === "error") {
          const error = JSON.parse(data) as { status: number; code: string; message: string; request_id?: string };
          throw new ApiError(error.status, error.code, error.message, error.request_id);
        }
      }
      if (done) throw new ApiError(0, "stream_interrupted", "Connection ended before a verified result arrived. Open the run status before retrying.");
    }
  } finally {
    await reader.cancel();
  }
}

export const api = {
  status: () => request<SystemStatus>("/system/status"),
  personas: () => request<Persona[]>("/personas"),
  policyMatrix: () => request<PolicyMatrix>("/policy/matrix"),
  patients: () => request<PatientListItem[]>("/patients"),
  patient: (id: string) => request<PatientDetail>(`/patients/${encodeURIComponent(id)}`),
  timeline: (id: string) => request<TimelineEvent[]>(`/patients/${encodeURIComponent(id)}/timeline`),
  document: (id: string, docId: string) =>
    request<DocumentDetail>(`/patients/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}`),
  risk: (id: string) => request<RiskResponse>(`/patients/${encodeURIComponent(id)}/risk`),
  retrieve: (id: string, body: { query: string; k?: number; mode?: string; date_from?: string; date_to?: string }) =>
    post<RetrievalResponse>(`/patients/${encodeURIComponent(id)}/retrieve`, body),
  runAgent: (body: { patient_id: string; question: string; session_id?: string | null }) =>
    post<AgentRun>("/agent/runs", body),
  runAgentStream,
  cancelRun: (runId: string) => post<{ cancel_requested: boolean }>(`/agent/runs/${encodeURIComponent(runId)}/cancel`, {}),
  run: (runId: string) => request<AgentRun>(`/agent/runs/${encodeURIComponent(runId)}`),
  recentRuns: () => request<RecentRun[]>("/agent/recent?limit=8"),
  feedback: (runId: string, body: { category: string; rating?: number; comment?: string }) =>
    post<{ status: string; note: string }>(`/agent/runs/${encodeURIComponent(runId)}/feedback`, body),
  submitReview: (runId: string) => post<Review>("/reviews", { run_id: runId }),
  reviews: () => request<Review[]>("/reviews"),
  review: (id: string) => request<Review>(`/reviews/${encodeURIComponent(id)}`),
  decide: (id: string, body: { decision: string; artifact_hash: string; comment?: string }) =>
    post<Review>(`/reviews/${encodeURIComponent(id)}/decision`, body),
  revise: (id: string, body: { remove_claims: number[]; note?: string }) =>
    post<Review>(`/reviews/${encodeURIComponent(id)}/revise`, body),
  audit: (decision?: string) =>
    request<{ counts: Record<string, number>; events: AuditEvent[] }>(`/audit${decision ? `?decision=${decision}` : ""}`),
  ops: () => request<OpsMetrics>("/ops/metrics"),
  evaluation: () => request<EvaluationReport>("/evaluation/latest"),
};
