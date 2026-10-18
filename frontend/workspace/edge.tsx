// Availability monitor for the public edge deployment.
//
// When the workspace is served by the Cloudflare edge, /edge/status reports whether the Python
// backend is ready, waking from idle or unavailable, plus the free inference budget. When it is
// served directly by FastAPI (local development, container), /edge/status does not exist and the
// monitor reports "direct" mode, in which the backend is by definition the server of this page.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { setTransientListener } from "./api";

export type BackendState = "ready" | "waking" | "unavailable" | "not_configured" | "unknown";

export interface EdgeStatus {
  edge: { status: string; release: string; served_at: string };
  backend: {
    state: Exclude<BackendState, "unknown">;
    latency_ms: number | null;
    version?: string;
    inference_mode?: string;
    inference_model?: string | null;
    readmission_model_loaded?: boolean;
    checked_at: string;
  };
  inference: {
    provider: string;
    model: string;
    configured: boolean;
    daily_neuron_budget: number;
    neurons_used_today: number;
    remaining_fraction: number;
    quota_state: "available" | "low" | "exhausted";
    resets_in_s: number;
  };
  limits: {
    agent_runs_today: number;
    agent_runs_global_per_day: number;
    agent_runs_per_visitor_per_day: number;
    remaining_agent_runs_today: number;
  };
}

export interface Availability {
  /** "edge": served by the public edge; "direct": served by the backend itself; "unknown": first check pending. */
  mode: "unknown" | "edge" | "direct";
  backend: BackendState;
  status: EdgeStatus | null;
  /** When the current not-ready period started (ms since epoch), for the elapsed-time display. */
  waitingSince: number | null;
  /** Increments each time the backend becomes ready after not being ready; data hooks refetch on change. */
  readyEpoch: number;
  checkNow: () => void;
}

const DEFAULT: Availability = { mode: "direct", backend: "ready", status: null, waitingSince: null, readyEpoch: 0, checkNow: () => undefined };
const AvailabilityContext = createContext<Availability>(DEFAULT);

export function isEdgeStatus(value: unknown): value is EdgeStatus {
  const v = value as Partial<EdgeStatus> | null;
  return Boolean(v && v.edge?.status === "ok" && v.backend && typeof v.backend.state === "string" && v.inference && v.limits);
}

export function AvailabilityProvider({ children, pollMs = { waiting: 3000, ready: 45000 } }: {
  children: ReactNode;
  pollMs?: { waiting: number; ready: number };
}) {
  const [state, setState] = useState<Omit<Availability, "checkNow">>({
    mode: "unknown", backend: "unknown", status: null, waitingSince: null, readyEpoch: 0,
  });
  const timer = useRef<number | null>(null);
  const inFlight = useRef(false);
  const stateRef = useRef(state);
  stateRef.current = state;

  const check = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    let next: Omit<Availability, "checkNow">;
    const prev = stateRef.current;
    try {
      const response = await fetch("/edge/status", { headers: { Accept: "application/json" }, cache: "no-store" });
      const body: unknown = response.ok && (response.headers.get("content-type") ?? "").includes("application/json")
        ? await response.json() : null;
      if (isEdgeStatus(body)) {
        const backend = body.backend.state;
        const ready = backend === "ready";
        next = {
          mode: "edge", backend, status: body,
          waitingSince: ready ? null : prev.waitingSince ?? Date.now(),
          readyEpoch: ready && prev.backend !== "ready" && prev.backend !== "unknown" ? prev.readyEpoch + 1 : prev.readyEpoch,
        };
      } else {
        next = { mode: "direct", backend: "ready", status: null, waitingSince: null,
          readyEpoch: prev.backend !== "ready" && prev.backend !== "unknown" ? prev.readyEpoch + 1 : prev.readyEpoch };
      }
    } catch {
      // The edge itself is unreachable (offline). Treat as unavailable and keep checking.
      next = { ...prev, mode: prev.mode === "unknown" ? "edge" : prev.mode, backend: "unavailable",
        waitingSince: prev.waitingSince ?? Date.now() };
    } finally {
      inFlight.current = false;
    }
    setState(next);
    if (timer.current !== null) window.clearTimeout(timer.current);
    if (next.mode === "direct" && next.backend === "ready") return; // nothing to monitor
    timer.current = window.setTimeout(() => void check(), next.backend === "ready" ? pollMs.ready : pollMs.waiting);
  }, [pollMs.ready, pollMs.waiting]);

  useEffect(() => {
    void check();
    setTransientListener(() => {
      // A request just failed transiently: re-check promptly (and start monitoring in direct mode).
      if (stateRef.current.backend === "ready") void check();
    });
    const onVisible = () => {
      if (document.visibilityState === "visible") void check();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      setTransientListener(null);
      document.removeEventListener("visibilitychange", onVisible);
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [check]);

  const value = useMemo<Availability>(() => ({ ...state, checkNow: () => void check() }), [state, check]);
  return <AvailabilityContext.Provider value={value}>{children}</AvailabilityContext.Provider>;
}

export function useAvailability(): Availability {
  return useContext(AvailabilityContext);
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${Math.round(seconds % 60)} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}
