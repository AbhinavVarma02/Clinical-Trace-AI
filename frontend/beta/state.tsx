// Beta-only state: hash router over the eight existing route names plus in-memory patient selection.
// Deliberately separate from the Stable AppProvider: no persona, no API persona header, no storage.
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { fixtureClient, type BetaClient } from "./client";

export const BETA_ROUTES = [
  { name: "overview", label: "Overview", description: "Readiness, guided demos, recent activity" },
  { name: "patients", label: "Patients", description: "Synthetic records, timeline, notes" },
  { name: "agent", label: "Case Review Agent", description: "Grounded questions and answers" },
  { name: "risk", label: "Risk & Explainability", description: "Readmission model and SHAP" },
  { name: "evidence", label: "Evidence Explorer", description: "Hybrid retrieval diagnostics" },
  { name: "evaluation", label: "Evaluation", description: "Offline quality and safety gates" },
  { name: "operations", label: "Operations", description: "Latency, errors, tools" },
  { name: "governance", label: "Governance", description: "Reviews, access control, audit" },
] as const;

export type BetaRouteName = (typeof BETA_ROUTES)[number]["name"];

export function parseRoute(hash: string): BetaRouteName {
  // Only the route name is read; the hash never carries patient ids, questions or consent.
  const path = hash.replace(/^#\/?/, "").split("?")[0] ?? "";
  return BETA_ROUTES.find((route) => route.name === path)?.name ?? "patients";
}

interface BetaState {
  client: BetaClient;
  route: BetaRouteName;
  alias: string | null;
  selectAlias: (alias: string | null) => void;
}

const BetaContext = createContext<BetaState | null>(null);

export function BetaProvider({ children, client = fixtureClient }: { children: ReactNode; client?: BetaClient }) {
  const [route, setRoute] = useState<BetaRouteName>(() => parseRoute(window.location.hash));
  const [alias, setAlias] = useState<string | null>(null);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const value = useMemo<BetaState>(() => ({ client, route, alias, selectAlias: setAlias }), [client, route, alias]);
  return <BetaContext.Provider value={value}>{children}</BetaContext.Provider>;
}

export function useBeta(): BetaState {
  const ctx = useContext(BetaContext);
  if (!ctx) throw new Error("useBeta must be used inside BetaProvider");
  return ctx;
}

export function navigateBeta(name: BetaRouteName): void {
  window.location.hash = `/${name}`;
}

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/** Load a value for `key`; a changed key discards the previous value so one patient never shows under another. */
export function useLoad<T>(load: () => Promise<T>, key: string | null): Loaded<T> {
  const [state, setState] = useState<{ key: string | null; data: T | null; error: string | null }>({ key: null, data: null, error: null });
  useEffect(() => {
    if (key === null) return;
    let active = true;
    load().then(
      (data) => active && setState({ key, data, error: null }),
      (err: unknown) => active && setState({ key, data: null, error: err instanceof Error ? err.message : "unavailable" }),
    );
    return () => {
      active = false;
    };
    // `load` is a fresh closure each render; the key identifies the request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const current = key !== null && state.key === key;
  return { data: current ? state.data : null, error: current ? state.error : null, loading: key !== null && !current };
}
