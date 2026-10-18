// Guarded gateway from the public edge to the Python backend.
//
// Only the routes the workspace uses are forwarded, with an allow-listed header set. Visitors are
// rate-limited per salted client key and agent runs are capped per client and globally per day.
// Backend cold starts and outages become typed JSON errors the UI can recover from.

import { limits, utcDay, type Env } from "./config";
import { clientKey, errorResponse } from "./http";
import { addCounter, recordEvent } from "./store";

const SEG = "[A-Za-z0-9_.:-]{1,128}";
type RouteKind = "read" | "write" | "agent";

const ROUTES: { method: string; pattern: RegExp; template: string; kind: RouteKind }[] = [
  ["GET", "/system/status", "read"],
  ["GET", "/personas", "read"],
  ["GET", "/policy/matrix", "read"],
  ["GET", "/tools", "read"],
  ["GET", "/patients", "read"],
  ["GET", "/patients/{id}", "read"],
  ["GET", "/patients/{id}/timeline", "read"],
  ["GET", "/patients/{id}/documents/{id}", "read"],
  ["GET", "/patients/{id}/resources/{id}/{id}/provenance", "read"],
  ["GET", "/patients/{id}/risk", "read"],
  ["POST", "/patients/{id}/retrieve", "write"],
  ["POST", "/agent/runs", "agent"],
  ["POST", "/agent/runs/stream", "agent"],
  ["GET", "/agent/runs/{id}/status", "read"],
  ["POST", "/agent/runs/{id}/resume", "agent"],
  ["GET", "/agent/runs/{id}", "read"],
  ["POST", "/agent/runs/{id}/cancel", "write"],
  ["POST", "/agent/runs/{id}/feedback", "write"],
  ["GET", "/agent/sessions/{id}", "read"],
  ["GET", "/agent/recent", "read"],
  ["POST", "/reviews", "write"],
  ["GET", "/reviews", "read"],
  ["GET", "/reviews/{id}", "read"],
  ["POST", "/reviews/{id}/decision", "write"],
  ["POST", "/reviews/{id}/revise", "write"],
  ["GET", "/audit", "read"],
  ["GET", "/ops/metrics", "read"],
  ["GET", "/evaluation/latest", "read"],
].map(([method, template, kind]) => ({
  method: method as string,
  template: `/api/v2${template}`,
  kind: kind as RouteKind,
  pattern: new RegExp(`^/api/v2${(template as string).replaceAll("{id}", SEG)}$`),
}));

const FORWARD_HEADERS = ["accept", "content-type", "x-demo-persona", "x-demo-workspace", "idempotency-key"];
const RETURN_HEADERS = ["content-type", "x-request-id", "retry-after", "cache-control"];
const MAX_BODY_BYTES = 32 * 1024;
const WORKSPACE_RE = /^[A-Za-z0-9_-]{16,64}$/;

export function matchRoute(method: string, path: string): { template: string; kind: RouteKind } | null {
  // /agent/runs/stream must not be swallowed by /agent/runs/{id}; ROUTES lists it first.
  for (const route of ROUTES) if (route.method === method && route.pattern.test(path)) return route;
  return null;
}

export type BackendFailure = "backend_waking" | "backend_unavailable" | "backend_timeout" | "backend_not_configured";

/** Classify a backend response that is not a usable JSON/SSE API response. */
export function classifyBackendResponse(status: number, contentType: string): BackendFailure | null {
  // JSON and SSE come from the application itself, including its own typed errors: pass through.
  if (contentType.includes("application/json") || contentType.includes("text/event-stream")) return null;
  // Sleeping or rebuilding hosts answer API paths with HTML holding pages or proxy 502/503/504.
  if (status === 502 || status === 503 || status === 504 || (status >= 200 && status < 300)) return "backend_waking";
  return "backend_unavailable";
}

export const FAILURE_MESSAGES: Record<BackendFailure, string> = {
  backend_waking: "The analysis backend is starting up after being idle. The workspace retries automatically.",
  backend_unavailable: "The analysis backend is unavailable right now. The workspace retries automatically.",
  backend_timeout: "The analysis backend did not answer in time. Try again in a few seconds.",
  backend_not_configured: "No analysis backend is configured for this deployment.",
};

export async function markWaking(env: Env): Promise<void> {
  await env.DB.prepare("INSERT OR IGNORE INTO edge_state(key, value, updated_at) VALUES ('wake_started', ?1, ?1)")
    .bind(new Date().toISOString()).run();
}

export async function handleApi(request: Request, env: Env, ctx: ExecutionContext, rid: string): Promise<Response> {
  const url = new URL(request.url);
  const route = matchRoute(request.method, url.pathname);
  if (!route) return errorResponse(404, "not_found", "This API route is not available through the public edge.", rid);
  if (!env.BACKEND_ORIGIN) return errorResponse(503, "backend_not_configured", FAILURE_MESSAGES.backend_not_configured, rid);

  const workspace = request.headers.get("x-demo-workspace");
  if (workspace !== null && !WORKSPACE_RE.test(workspace)) {
    return errorResponse(400, "invalid_workspace", "The demo workspace identifier is malformed.", rid);
  }

  const day = utcDay();
  const client = await clientKey(request, env.EDGE_BACKEND_SECRET ?? "edge", day);
  const general = env.API_LIMITER ? await env.API_LIMITER.limit({ key: client }) : { success: true };
  if (!general.success) {
    ctx.waitUntil(addCounter(env.DB, day, "rate_limited", "", 1).then(() => undefined));
    return errorResponse(429, "rate_limited", "Too many requests from this connection. Wait a minute and retry.", rid,
      { retry_after_s: 60 }, { "retry-after": "60" });
  }

  if (route.kind === "agent") {
    const burst = env.AGENT_LIMITER ? await env.AGENT_LIMITER.limit({ key: client }) : { success: true };
    if (!burst.success) {
      return errorResponse(429, "rate_limited", "Agent runs are limited per minute for each visitor. Wait a moment and retry.", rid,
        { retry_after_s: 60 }, { "retry-after": "60" });
    }
    const cap = limits(env);
    const [mine, all] = await Promise.all([
      addCounter(env.DB, day, "agent_runs", client, 1),
      addCounter(env.DB, day, "agent_runs", "", 1),
    ]);
    if (mine > cap.agentRunsPerClient || all > cap.agentRunsGlobal) {
      const scope = mine > cap.agentRunsPerClient ? "visitor" : "deployment";
      return errorResponse(429, "daily_limit", `The free daily agent-run limit for this ${scope} is reached. It resets at 00:00 UTC.`, rid,
        { scope });
    }
  }

  let body: ArrayBuffer | undefined;
  if (request.method === "POST") {
    const declared = Number(request.headers.get("content-length") ?? "0");
    if (declared > MAX_BODY_BYTES) return errorResponse(413, "payload_too_large", "Request body is too large.", rid);
    body = await request.arrayBuffer();
    if (body.byteLength > MAX_BODY_BYTES) return errorResponse(413, "payload_too_large", "Request body is too large.", rid);
  }

  const headers = new Headers();
  for (const name of FORWARD_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  headers.set("x-request-id", rid);
  headers.set("x-edge-client", client);
  if (env.EDGE_BACKEND_SECRET) headers.set("x-edge-secret", env.EDGE_BACKEND_SECRET);

  const target = new URL(url.pathname + url.search, env.BACKEND_ORIGIN);
  const started = Date.now();
  const streaming = url.pathname.endsWith("/stream");
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
      signal: AbortSignal.timeout(streaming ? 45_000 : 30_000),
    });
  } catch (err) {
    const timeout = err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError");
    const code: BackendFailure = timeout ? "backend_timeout" : "backend_unavailable";
    ctx.waitUntil(Promise.all([
      addCounter(env.DB, day, `error:${code}`, "", 1),
      recordEvent(env.DB, day, { kind: route.kind === "agent" ? "agent" : "api", route: route.template, status: 503, outcome: code, latencyMs: Date.now() - started }),
      markWaking(env),
    ]).then(() => undefined));
    return errorResponse(503, code, FAILURE_MESSAGES[code], rid, { retry_after_s: 5 }, { "retry-after": "5" });
  }

  const contentType = upstream.headers.get("content-type") ?? "";
  const failure = classifyBackendResponse(upstream.status, contentType);
  if (failure) {
    ctx.waitUntil(Promise.all([
      upstream.body?.cancel(),
      addCounter(env.DB, day, `error:${failure}`, "", 1),
      recordEvent(env.DB, day, { kind: route.kind === "agent" ? "agent" : "api", route: route.template, status: upstream.status, outcome: failure, latencyMs: Date.now() - started }),
      markWaking(env),
    ]).then(() => undefined));
    return errorResponse(503, failure, FAILURE_MESSAGES[failure], rid, { retry_after_s: 5 }, { "retry-after": "5" });
  }

  const out = new Headers();
  for (const name of RETURN_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) out.set(name, value);
  }
  out.set("x-request-id", upstream.headers.get("x-request-id") ?? rid);
  out.set("x-content-type-options", "nosniff");
  if (!out.has("cache-control")) out.set("cache-control", "no-store");

  const counted = addCounter(env.DB, day, upstream.status >= 400 ? `status:${upstream.status}` : "api_ok", "", 1);
  if (route.kind !== "agent" || !upstream.body) {
    ctx.waitUntil(Promise.all([counted, upstream.status >= 500
      ? recordEvent(env.DB, day, { kind: "api", route: route.template, status: upstream.status, outcome: "backend_error", latencyMs: Date.now() - started })
      : undefined]).then(() => undefined));
    return new Response(upstream.body, { status: upstream.status, headers: out });
  }

  // Agent runs: measure full completion time (including streamed progress) without buffering.
  if (streaming) out.set("x-accel-buffering", "no");
  const { readable, writable } = new TransformStream();
  const status = upstream.status;
  ctx.waitUntil(upstream.body.pipeTo(writable).then(
    () => "completed",
    () => "client_disconnected",
  ).then((outcome) => Promise.all([counted, recordEvent(env.DB, day, {
    kind: "agent", route: route.template, status, outcome: status >= 400 ? `http_${status}` : outcome, latencyMs: Date.now() - started,
  })])).then(() => undefined));
  return new Response(readable, { status, headers: out });
}
