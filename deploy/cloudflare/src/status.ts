// Public availability and measured edge metrics. Aggregates only; no identifiers or content.

import { limits, utcDay, type Env } from "./config";
import { json } from "./http";
import { inferenceSummary } from "./inference";
import { getCounter, recordEvent } from "./store";

export type BackendState = "ready" | "waking" | "unavailable" | "not_configured";

interface Probe {
  state: BackendState;
  latency_ms: number | null;
  version?: string;
  inference_mode?: string;
  inference_model?: string | null;
  readmission_model_loaded?: boolean;
  checked_at: string;
}

let cached: { at: number; probe: Probe } | null = null;
const PROBE_TTL_MS = 4000;

export async function probeBackend(env: Env, ctx: ExecutionContext): Promise<Probe> {
  if (cached && Date.now() - cached.at < PROBE_TTL_MS) return cached.probe;
  const checked_at = new Date().toISOString();
  if (!env.BACKEND_ORIGIN) return { state: "not_configured", latency_ms: null, checked_at };
  const started = Date.now();
  let probe: Probe;
  try {
    const headers: HeadersInit = env.EDGE_BACKEND_SECRET ? { "x-edge-secret": env.EDGE_BACKEND_SECRET } : {};
    const res = await fetch(new URL("/api/v2/system/status", env.BACKEND_ORIGIN), { headers, signal: AbortSignal.timeout(6000) });
    const latency = Date.now() - started;
    const type = res.headers.get("content-type") ?? "";
    if (res.ok && type.includes("application/json")) {
      const body = (await res.json()) as Record<string, any>;
      probe = {
        state: "ready", latency_ms: latency, checked_at, version: body.version,
        inference_mode: body.inference?.mode, inference_model: body.inference?.provider?.model ?? null,
        readmission_model_loaded: Boolean(body.readmission_model?.loaded),
      };
    } else {
      await res.body?.cancel();
      probe = { state: res.status === 404 ? "unavailable" : "waking", latency_ms: latency, checked_at };
    }
  } catch {
    probe = { state: "waking", latency_ms: Date.now() - started, checked_at };
  }
  ctx.waitUntil(trackRecovery(env, probe.state));
  cached = { at: Date.now(), probe };
  return probe;
}

/** Record how long a cold start took, from the first failed request until the backend was ready. */
async function trackRecovery(env: Env, state: BackendState): Promise<void> {
  if (state === "ready") {
    const row = await env.DB.prepare("DELETE FROM edge_state WHERE key = 'wake_started' RETURNING value").first<{ value: string }>();
    if (row) {
      await recordEvent(env.DB, utcDay(), { kind: "wake", route: "backend", status: 200, outcome: "recovered",
        latencyMs: Date.now() - Date.parse(row.value) });
    }
  } else if (state === "waking") {
    await env.DB.prepare("INSERT OR IGNORE INTO edge_state(key, value, updated_at) VALUES ('wake_started', ?1, ?1)")
      .bind(new Date().toISOString()).run();
  }
}

export async function handleStatus(env: Env, ctx: ExecutionContext): Promise<Response> {
  const day = utcDay();
  const cap = limits(env);
  const [backend, inference, runsToday] = await Promise.all([
    probeBackend(env, ctx),
    inferenceSummary(env),
    getCounter(env.DB, day, "agent_runs"),
  ]);
  return json({
    edge: { status: "ok", release: env.RELEASE, served_at: new Date().toISOString() },
    backend,
    inference,
    limits: {
      agent_runs_today: runsToday,
      agent_runs_global_per_day: cap.agentRunsGlobal,
      agent_runs_per_visitor_per_day: cap.agentRunsPerClient,
      remaining_agent_runs_today: Math.max(0, cap.agentRunsGlobal - runsToday),
    },
    disclosure: "Synthetic data only. Educational prototype; not clinically validated.",
  });
}

export function percentile(values: number[], pct: number): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil((pct / 100) * sorted.length) - 1));
  return sorted[index]!;
}

export async function handleMetrics(env: Env): Promise<Response> {
  const since = new Date(Date.now() - 7 * 86_400_000).toISOString().slice(0, 10);
  const [counters, events] = await Promise.all([
    env.DB.prepare("SELECT day, name, SUM(value) AS value FROM daily_counter WHERE day >= ?1 AND subject = '' GROUP BY day, name")
      .bind(since).all<{ day: string; name: string; value: number }>(),
    env.DB.prepare("SELECT kind, outcome, status, latency_ms FROM edge_event WHERE day >= ?1 ORDER BY id DESC LIMIT 3000")
      .bind(since).all<{ kind: string; outcome: string; status: number; latency_ms: number }>(),
  ]);
  const totals: Record<string, number> = {};
  for (const row of counters.results) totals[row.name] = (totals[row.name] ?? 0) + row.value;
  const rows = events.results;
  const latencies = (kind: string, ok: (r: (typeof rows)[number]) => boolean) =>
    rows.filter((r) => r.kind === kind && ok(r)).map((r) => r.latency_ms);
  const agentDone = latencies("agent", (r) => r.outcome === "completed");
  const llmOk = latencies("llm", (r) => r.outcome === "ok");
  const wakes = latencies("wake", () => true);
  const okRequests = totals["api_ok"] ?? 0;
  const failures = Object.entries(totals).filter(([k]) => k.startsWith("error:") || k.startsWith("status:5"))
    .reduce((n, [, v]) => n + v, 0);
  const clientErrors = Object.entries(totals).filter(([k]) => k.startsWith("status:4")).reduce((n, [, v]) => n + v, 0);
  const total = okRequests + failures + clientErrors;
  return json({
    window: { since, until: utcDay(), note: "Measured at the public edge; percentiles over the most recent 3,000 events." },
    requests: { total, ok: okRequests, client_errors: clientErrors, server_or_availability_errors: failures,
      availability_error_rate: total ? Number((failures / total).toFixed(4)) : null,
      rate_limited: totals["rate_limited"] ?? 0 },
    errors_by_code: Object.fromEntries(Object.entries(totals).filter(([k]) => k.startsWith("error:") || k.startsWith("status:"))),
    agent_runs: { started: totals["agent_runs"] ?? 0, completed_measured: agentDone.length,
      completion_p50_ms: percentile(agentDone, 50), completion_p95_ms: percentile(agentDone, 95) },
    inference: { calls: totals["llm_calls"] ?? 0, neurons: Number((totals["neurons"] ?? 0).toFixed(1)),
      latency_p50_ms: percentile(llmOk, 50), latency_p95_ms: percentile(llmOk, 95),
      quota_rejections: rows.filter((r) => r.kind === "llm" && r.status === 429).length,
      provider_errors: rows.filter((r) => r.kind === "llm" && r.outcome === "provider_error").length },
    cold_starts: { recoveries: wakes.length, recovery_p50_ms: percentile(wakes, 50), recovery_max_ms: wakes.length ? Math.max(...wakes) : null },
  });
}
