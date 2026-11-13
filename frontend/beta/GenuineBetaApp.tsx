import { useCallback, useEffect, useRef, useState } from "react";
import { BetaShell } from "./BetaShell";
import { CandidateSummary } from "./CandidateSummary";
import type { BetaView } from "./contract";
import {
  completeOwnerEnrollment, genuineOwnerActions, genuineOwnerOperation, genuineOwnerRead, pendingOwnerEnrollment,
  type CandidateResult, type OwnerAction, type PendingEnrollment,
} from "./genuine";
import { Provenance, RecordTabs, RefreshStatus, WarningList } from "./PatientRecord";
import { BETA_ROUTES, navigateBeta, parseRoute, useBetaRoute } from "./routing";
import { Card, EmptyState } from "./ui";
import { OWNER_ORIGIN } from "./webauthn";

const INITIAL_NOTICE = "Owner verification is required for each protected request.";
type Operation = "verify" | "read" | "pending" | "enroll" | "actions" | OwnerAction;
const OPERATION_LABELS: Record<OwnerAction, string> = {
  acquire: "Acquire approved Epic patient once",
  review: "Review staged candidate",
  promote: "Promote reviewed candidate",
};

export function GenuineBetaApp() {
  const route = useBetaRoute();
  const [view, setView] = useState<BetaView | null>(null);
  const [candidate, setCandidate] = useState<{ result: CandidateResult; expiresAt: number } | null>(null);
  const [actions, setActions] = useState<OwnerAction[]>([]);
  const [pending, setPending] = useState<PendingEnrollment | null>(null);
  const [notice, setNotice] = useState(INITIAL_NOTICE);
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const retained = useRef(false);
  const expiry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const command = useRef<AbortController | null>(null);

  const clear = useCallback(() => {
    command.current?.abort();
    command.current = null;
    sequence.current += 1;
    retained.current = false;
    if (expiry.current) clearTimeout(expiry.current);
    expiry.current = null;
    setView(null);
    setCandidate(null);
    setPending(null);
    setBusy(false);
  }, []);

  const clearWorkspace = useCallback(() => {
    clear();
    setActions([]);
    setNotice(INITIAL_NOTICE);
  }, [clear]);

  useEffect(() => {
    const hide = () => {
      // Native Hello may change focus between requests while the browser retains no protected result.
      if (retained.current || document.visibilityState === "hidden") {
        clear();
        setNotice("View cleared. Verify again to read.");
      }
    };
    let currentRoute = parseRoute(window.location.hash);
    const changeRoute = (event: HashChangeEvent) => {
      const next = parseRoute(event.newURL ? new URL(event.newURL).hash : window.location.hash);
      if (next !== currentRoute) clearWorkspace();
      currentRoute = next;
    };
    window.addEventListener("blur", hide);
    window.addEventListener("focus", hide);
    window.addEventListener("hashchange", changeRoute);
    window.addEventListener("pagehide", clearWorkspace);
    document.addEventListener("visibilitychange", hide);
    return () => {
      sequence.current += 1;
      command.current?.abort();
      if (expiry.current) clearTimeout(expiry.current);
      window.removeEventListener("blur", hide);
      window.removeEventListener("focus", hide);
      window.removeEventListener("hashchange", changeRoute);
      window.removeEventListener("pagehide", clearWorkspace);
      document.removeEventListener("visibilitychange", hide);
    };
  }, [clear, clearWorkspace]);

  async function act(operation: Operation) {
    if (command.current) return;
    const enrollment = pending;
    const reviewed = candidate?.result.operation === "review" && candidate.expiresAt > Date.now() ? candidate.result.provenance : null;
    clear();
    const controller = new AbortController();
    command.current = controller;
    const revision = sequence.current;
    const isCurrent = (deadline = Infinity) => (
      revision === sequence.current && parseRoute(window.location.hash) === route &&
      document.visibilityState === "visible" && document.hasFocus() && deadline > Date.now()
    );
    const showUntil = (expiresAt: number) => {
      retained.current = true;
      expiry.current = setTimeout(() => {
        clear();
        setNotice("View expired. Verify again to read.");
      }, Math.max(0, expiresAt - Date.now()));
    };
    setBusy(true);
    setNotice(operation === "actions" ? "Checking approved operator actions…" : "Waiting for owner verification…");
    try {
      if (operation === "actions") {
        setActions([]);
        const available = await genuineOwnerActions(controller.signal);
        if (!isCurrent()) return;
        setActions(available);
        setNotice(available.length ? "Choose an approved action. Each action requires a fresh owner proof." : "No operator actions are enabled for this listener.");
      } else if (operation === "pending") {
        const value = await pendingOwnerEnrollment(controller.signal);
        if (!isCurrent()) return;
        setPending(value);
        setNotice("Compare the entire fingerprint in the approved owner console, then complete enrollment.");
      } else if (operation === "enroll" && enrollment) {
        await completeOwnerEnrollment(enrollment, controller.signal);
        if (isCurrent()) setNotice("Enrolled. A fresh proof is required to verify or read.");
      } else if (operation === "verify" || operation === "read") {
        const value = await genuineOwnerRead(operation, controller.signal);
        if (!isCurrent(value.expiresAt)) return;
        if (operation === "read" && "record" in value.result) {
          showUntil(value.expiresAt);
          setView(value.result);
        }
        setNotice(operation === "read" ? "Verified read. The view clears within 30 seconds or when leaving this window." : "Owner proof verified. No reusable session was created.");
      } else if (operation === "acquire" || operation === "review" || operation === "promote") {
        if (route !== "operations" || !actions.includes(operation) || (operation === "promote" && !reviewed)) return;
        if (operation !== "review") setActions((current) => current.filter((action) => action !== operation));
        const value = await genuineOwnerOperation(operation, reviewed, controller.signal);
        if (!isCurrent(value.expiresAt)) return;
        showUntil(value.expiresAt);
        setCandidate(value);
        setNotice("Verified operator result. This view clears within 30 seconds or when leaving this window.");
      }
    } catch {
      if (revision === sequence.current) {
        clear();
        setNotice("Request refused or cancelled. No patient content is retained.");
      }
    } finally {
      if (command.current === controller) {
        command.current = null;
        setBusy(false);
      }
    }
  }

  if (window.location.origin !== OWNER_ORIGIN || !window.isSecureContext) return <main>Trusted owner HTTPS is required.</main>;

  const clearButton = <button type="button" className="btn" onClick={() => { clear(); setNotice("View cleared."); }}>Clear view</button>;
  const recordPage = route === "patients" || route === "evidence";
  const ownerPage = route === "overview" || route === "patients";
  return (
    <BetaShell route={route} owner onNavigate={clearWorkspace}>
      <div className="stack" style={{ gap: "var(--space-4)" }}>
        <div className="page-header">
          <div>
            <h1 className="page-title">{BETA_ROUTES.find((item) => item.name === route)?.label}</h1>
            <p className="muted">Epic Beta · local owner workspace</p>
          </div>
        </div>

        {route === "overview" && (
          <Card title="Epic sandbox record" subtitle="One approved synthetic patient">
            <div className="stack">
              <p>The Patients page shows the verified record, source coverage and provenance after a fresh owner proof.</p>
              <div><button type="button" className="btn" onClick={() => { clearWorkspace(); navigateBeta("patients"); }}>Open Patients</button></div>
            </div>
          </Card>
        )}

        {(ownerPage || recordPage) && (
          <Card title={route === "evidence" ? "Source evidence" : "Owner access"}>
            <div className="stack">
              <div className="row" aria-busy={busy}>
                {ownerPage && <button type="button" className="btn" disabled={busy} onClick={() => void act("verify")}>Verify Windows Hello</button>}
                {recordPage && <button type="button" className="btn primary" disabled={busy} onClick={() => void act("read")}>{route === "patients" ? "Read approved patient" : "Read approved source evidence"}</button>}
                {clearButton}
                {ownerPage && <button type="button" className="btn" disabled={busy} onClick={() => void act("pending")}>Request owner enrollment</button>}
                {ownerPage && pending && <button type="button" className="btn" disabled={busy} onClick={() => void act("enroll")}>Complete approved enrollment</button>}
              </div>
              <p role="status">{notice}</p>
              {ownerPage && pending && <p>Enrollment fingerprint <code>{pending.fingerprint}</code></p>}
              {route === "evidence" && <p className="muted">Source coverage and provenance are available for a verified generation. Clinical answer generation and citations are disabled.</p>}
            </div>
          </Card>
        )}

        {recordPage && view?.record && (
          <>
            <WarningList warnings={view.warnings} />
            {route === "patients" && <RecordTabs key={view.active?.generation_id} record={view.record} view={view} />}
            <RefreshStatus view={view} />
            <Provenance view={view} />
          </>
        )}

        {route === "operations" && (
          <>
            <Card title="Epic operator actions">
              <div className="stack">
                <p>Actions depend on the approved listener phase. Clinical AI and publication are disabled.</p>
                <div className="row" aria-busy={busy}>
                  <button type="button" className="btn" disabled={busy} onClick={() => void act("actions")}>Check approved operator actions</button>
                  {actions.map((action) => (
                    <button key={action} type="button" className="btn" disabled={busy || (action === "promote" && candidate?.result.operation !== "review")} onClick={() => void act(action)}>
                      {OPERATION_LABELS[action]}
                    </button>
                  ))}
                  {clearButton}
                </div>
                <p role="status">{notice}</p>
              </div>
            </Card>
            {candidate && <CandidateSummary candidate={candidate.result} />}
          </>
        )}

        {!ownerPage && !recordPage && route !== "operations" && (
          <EmptyState title="Workflow disabled in Epic Beta">This milestone provides read-only access to the approved Epic sandbox record.</EmptyState>
        )}
      </div>
    </BetaShell>
  );
}
