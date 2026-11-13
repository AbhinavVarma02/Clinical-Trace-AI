// Beta-only state: hash router over the eight existing route names plus in-memory patient selection.
// Deliberately separate from the Stable AppProvider: no persona, no API persona header, no storage.
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { BetaApiError, fixtureClient, type BetaClient } from "./client";
import type { Answer, BetaView } from "./contract";
import { useBetaRoute, type BetaRouteName } from "./routing";

export { BETA_ROUTES, parseRoute } from "./routing";

export interface RetainedRun {
  alias: string;
  generationId: string;
  sessionBinding: string;
  answer: Answer;
}

interface BetaState {
  client: BetaClient;
  /** Delay between status reads while a refresh runs. */
  pollMs: number;
  route: BetaRouteName;
  alias: string | null;
  selectAlias: (alias: string | null) => void;
  latestRun: RetainedRun | null;
  /** Invalidates pending captures too, so late answers cannot restore cleared evidence. */
  clearRun: () => number;
  retainRun: (run: RetainedRun, revision: number) => void;
  /** True after a successful sign-out until the page is reloaded; the app then shows only the sign-in card. */
  signedOut: boolean;
  /** Undefined when the client cannot sign out (the fixture client). */
  signOut?: () => Promise<void>;
}

const BetaContext = createContext<BetaState | null>(null);

export function BetaProvider({ children, client = fixtureClient, pollMs = 1000 }: { children: ReactNode; client?: BetaClient; pollMs?: number }) {
  const route = useBetaRoute();
  const [alias, setAlias] = useState<string | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [latestRun, setLatestRun] = useState<RetainedRun | null>(null);
  const revision = useRef(0);
  const selected = useRef<string | null>(null);

  const clearRun = () => {
    revision.current += 1;
    setLatestRun(null);
    return revision.current;
  };

  const value = useMemo<BetaState>(
    () => ({
      client,
      pollMs,
      route,
      alias,
      selectAlias: (next) => {
        if (selected.current !== next) clearRun();
        selected.current = next;
        setAlias(next);
      },
      latestRun,
      clearRun,
      retainRun: (run, capturedRevision) => {
        if (capturedRevision === revision.current && selected.current === run.alias) setLatestRun(run);
      },
      signedOut,
      signOut: client.signOut
        ? async () => {
            clearRun();
            await client.signOut!();
            clearRun();
            selected.current = null;
            setAlias(null); // nothing of the previous session stays in memory
            setSignedOut(true);
          }
        : undefined,
    }),
    [client, pollMs, route, alias, signedOut, latestRun],
  );
  return <BetaContext.Provider value={value}>{children}</BetaContext.Provider>;
}

export function useBeta(): BetaState {
  const ctx = useContext(BetaContext);
  if (!ctx) throw new Error("useBeta must be used inside BetaProvider");
  return ctx;
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

/** Passive record admission and revalidation. Clear the view on every access failure, expiry or foreground
 * transition; a late response cannot restore content after an invalidation. Reads never renew the session. */
export function usePatientView(alias: string): Loaded<BetaView> & { reload: () => Promise<BetaView | null>; invalidate: (error: string) => void } {
  const { client, pollMs, clearRun } = useBeta();
  const [state, setState] = useState<{ alias: string; client: BetaClient; data: BetaView | null; error: string | null } | null>(null);
  const reload = useRef<() => Promise<BetaView | null>>(async () => null);
  const invalidate = useRef<(error: string) => void>(() => undefined);
  useEffect(() => {
    let active = true;
    let sequence = 0;
    let binding: string | null = null;
    let generation: string | null = null;
    let poll: ReturnType<typeof setTimeout>;
    let expiry: ReturnType<typeof setTimeout>;
    const hide = (error: string | null) => {
      setState({ alias, client, data: null, error });
      clearRun();
    };
    const armExpiry = (expiresAt: number) => {
      clearTimeout(expiry);
      expiry = setTimeout(() => {
        if (!active) return;
        sequence += 1;
        clearTimeout(poll);
        hide("owner_required");
      }, Math.max(0, expiresAt - Date.now()));
    };
    const check = async (): Promise<BetaView | null> => {
      const request = ++sequence;
      clearTimeout(poll);
      try {
        const before = await client.getSession?.();
        if (!active || request !== sequence) return null;
        if (before) {
          if (before.expiresAt <= Date.now() || (binding !== null && before.binding !== binding)) throw new BetaApiError(401, "owner_required");
          binding = before.binding;
          armExpiry(before.expiresAt);
        }
        const data = await client.getView(alias);
        const after = await client.getSession?.();
        if (!active || request !== sequence) return null;
        if (before && (!after || before.binding !== after.binding || Math.min(before.expiresAt, after.expiresAt) <= Date.now())) throw new BetaApiError(401, "owner_required");
        if (data.alias !== alias || (data.provenance && data.provenance.generation_id !== data.active?.generation_id)) throw new BetaApiError(409, "record_mismatch");
        if (generation !== data.active?.generation_id) clearRun();
        generation = data.active?.generation_id ?? null;
        setState({ alias, client, data, error: null });
        if (before && after) {
          armExpiry(Math.min(before.expiresAt, after.expiresAt));
          poll = setTimeout(() => void check(), Math.max(100, pollMs * 5));
        }
        return data;
      } catch (failure) {
        if (active && request === sequence) {
          clearTimeout(expiry);
          hide(failure instanceof BetaApiError ? failure.code : "unavailable");
        }
        return null;
      }
    };
    reload.current = check;
    invalidate.current = (error) => {
      sequence += 1;
      clearTimeout(poll);
      clearTimeout(expiry);
      if (active) hide(error);
    };
    const foreground = () => {
      sequence += 1;
      clearTimeout(poll);
      hide(null);
      if (document.visibilityState !== "hidden") void check();
    };
    void check();
    if (client.getSession) {
      window.addEventListener("focus", foreground);
      document.addEventListener("visibilitychange", foreground);
    }
    return () => {
      active = false;
      clearTimeout(poll);
      clearTimeout(expiry);
      window.removeEventListener("focus", foreground);
      document.removeEventListener("visibilitychange", foreground);
    };
    // clearRun changes with context renders; the client/alias identify this admission.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, alias, pollMs]);
  const current = state?.alias === alias && state.client === client ? state : null;
  return { data: current?.data ?? null, error: current?.error ?? null, loading: !current || (!current.data && !current.error), reload: () => reload.current(), invalidate: (error) => invalidate.current(error) };
}
