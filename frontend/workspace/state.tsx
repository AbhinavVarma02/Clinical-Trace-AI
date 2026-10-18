// App-wide state: persona, theme, selected patient, and a tiny hash router.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, setApiPersona } from "./api";
import { useAvailability } from "./edge";

export type Theme = "light" | "dark";
export type RouteName = "overview" | "patients" | "agent" | "risk" | "evidence" | "evaluation" | "operations" | "governance";

export const ROUTES: { name: RouteName; label: string; description: string }[] = [
  { name: "overview", label: "Overview", description: "Readiness, guided demos, recent activity" },
  { name: "patients", label: "Patients", description: "Synthetic records, timeline, notes" },
  { name: "agent", label: "Case Review Agent", description: "Grounded questions and answers" },
  { name: "risk", label: "Risk & Explainability", description: "Readmission model and SHAP" },
  { name: "evidence", label: "Evidence Explorer", description: "Hybrid retrieval diagnostics" },
  { name: "evaluation", label: "Evaluation", description: "Offline quality and safety gates" },
  { name: "operations", label: "Operations", description: "Latency, errors, tools" },
  { name: "governance", label: "Governance", description: "Reviews, access control, audit" },
];

export interface Route {
  name: RouteName;
  params: URLSearchParams;
}

function parseHash(hash: string): Route {
  const clean = hash.replace(/^#\/?/, "");
  const [path = "", query = ""] = clean.split("?");
  const name = (ROUTES.find((r) => r.name === path)?.name ?? "overview") as RouteName;
  return { name, params: new URLSearchParams(query) };
}

export function navigate(name: RouteName, params: Record<string, string> = {}): void {
  const query = new URLSearchParams(params).toString();
  window.location.hash = `/${name}${query ? `?${query}` : ""}`;
}

/** Like navigate, but replaces the current history entry (for consuming one-shot parameters). */
export function replaceRoute(name: RouteName, params: Record<string, string> = {}): void {
  const query = new URLSearchParams(params).toString();
  window.location.replace(`#/${name}${query ? `?${query}` : ""}`);
}

function readStorage(key: string, fallback: string): string {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage may be unavailable (private mode); preferences then last for the session only.
  }
}

interface AppState {
  persona: string;
  setPersona: (persona: string) => void;
  theme: Theme;
  toggleTheme: () => void;
  patientId: string;
  setPatientId: (patientId: string) => void;
  route: Route;
}

const AppContext = createContext<AppState | null>(null);

function initialTheme(): Theme {
  const stored = readStorage("ct-theme", "");
  if (stored === "light" || stored === "dark") return stored;
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [persona, setPersonaState] = useState(() => readStorage("ct-persona", "clinician.demo"));
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [patientId, setPatientIdState] = useState(() => readStorage("ct-patient", "synthetic_p001"));
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  setApiPersona(persona);

  useEffect(() => {
    const onHash = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    writeStorage("ct-theme", theme);
  }, [theme]);

  const setPersona = useCallback((value: string) => {
    setApiPersona(value);
    writeStorage("ct-persona", value);
    setPersonaState(value);
  }, []);

  const setPatientId = useCallback((value: string) => {
    writeStorage("ct-patient", value);
    setPatientIdState(value);
  }, []);

  const value = useMemo<AppState>(
    () => ({
      persona,
      setPersona,
      theme,
      toggleTheme: () => setTheme((t) => (t === "light" ? "dark" : "light")),
      patientId,
      setPatientId,
      route,
    }),
    [persona, setPersona, theme, patientId, setPatientId, route],
  );
  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used inside AppProvider");
  return ctx;
}

export interface ApiState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Fetch on mount and whenever `key` (or the persona) changes; stale responses are ignored.
 * Data is also refetched when the backend becomes ready again after a cold start or outage.
 */
export function useApi<T>(fetcher: () => Promise<T>, key: string, enabled = true): ApiState<T> {
  const { persona } = useApp();
  const { readyEpoch } = useAvailability();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [nonce, setNonce] = useState(0);
  const scope = `${persona}:${key}:${nonce}:${enabled}:${readyEpoch}`;
  const [loadedScope, setLoadedScope] = useState("");
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    setData(null);
    setError(null);
    setLoadedScope(scope);
    if (!enabled) {
      setLoading(false);
      return;
    }
    let active = true;
    setLoading(true);
    setError(null);
    fetcherRef
      .current()
      .then((result) => {
        if (active) setData(result);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setData(null);
        setError(err instanceof ApiError ? err : new ApiError(0, "unknown", String(err)));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [key, persona, nonce, enabled, readyEpoch]);

  const current = enabled && loadedScope === scope;
  return { data: current ? data : null, error: current ? error : null,
    loading: enabled && (!current || loading), reload: () => setNonce((n) => n + 1) };
}
