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
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let currentPersona = "clinician.demo";

export function setApiPersona(persona: string): void {
  currentPersona = persona;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-Demo-Persona", currentPersona);
  if (init.body) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`/api/v2${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "network_error", "The API could not be reached. Check that the server is running.");
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
    throw new ApiError(response.status, body.error?.code ?? `http_${response.status}`, message, body.error?.request_id);
  }
  return (await response.json()) as T;
}

const post = <T>(path: string, body: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body) });

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
