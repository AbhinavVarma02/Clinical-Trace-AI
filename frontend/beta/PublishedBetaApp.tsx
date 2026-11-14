import { useEffect, useState } from "react";
import { BetaShell } from "./BetaShell";
import { DataOrigin, Provenance, RecordTabs, RefreshStatus, WarningList } from "./PatientRecord";
import { loadPublication, type Publication } from "./published";
import { BETA_ROUTES, navigateBeta, useBetaRoute } from "./routing";
import { Card, EmptyState, formatUnix } from "./ui";

// Hosted read-only demonstration of one owner-approved publication copy. No owner endpoint, WebAuthn,
// cookie, storage, polling or clinical AI: the copy is loaded once and shown only after its checks pass.
export function PublishedBetaApp() {
  const route = useBetaRoute();
  const [state, setState] = useState<{ publication: Publication } | { failed: true } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    loadPublication(controller.signal).then(
      (publication) => setState({ publication }),
      () => { if (!controller.signal.aborted) setState({ failed: true }); },
    );
    return () => controller.abort();
  }, []);

  const view = state && "publication" in state ? state.publication.view : null;
  const recordPage = route === "patients" || route === "evidence";
  return (
    <BetaShell route={route} published>
      <div className="stack" style={{ gap: "var(--space-4)" }}>
        <div className="page-header">
          <div>
            <h1 className="page-title">{BETA_ROUTES.find((item) => item.name === route)?.label}</h1>
            <p className="muted">Epic Beta · published read-only copy</p>
          </div>
        </div>

        {route === "overview" && (
          <Card title="Epic sandbox record" subtitle="One approved synthetic patient, as retrieved once">
            <div className="stack">
              {view && <div className="row"><DataOrigin view={view} /></div>}
              <p>
                The Patients page shows the record exactly as the owner-approved retrieval returned it, with source coverage,
                known limitations and provenance hashes. The Evidence Explorer shows coverage and provenance only.
              </p>
              {view?.active && <p className="muted">Retrieved {formatUnix(view.active.retrieved_at)} UTC. This copy is never refreshed.</p>}
              <div><button type="button" className="btn" onClick={() => navigateBeta("patients")}>Open Patients</button></div>
            </div>
          </Card>
        )}

        {recordPage && state === null && <p role="status">Loading the published record…</p>}
        {recordPage && state && "failed" in state && (
          <EmptyState title="Published record unavailable">Nothing is shown unless the published copy passes every check.</EmptyState>
        )}
        {route === "evidence" && view && (
          <Card title="Source evidence">
            <p className="muted">Source coverage and provenance for the published generation. Clinical answer generation and citations are disabled.</p>
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
          <EmptyState title="No operator actions in the hosted demonstration">
            Acquisition, review and promotion run only on the owner's workstation behind Windows Hello. This site has no owner functions.
          </EmptyState>
        )}

        {!recordPage && route !== "overview" && route !== "operations" && (
          <EmptyState title="Workflow disabled in Epic Beta">This milestone provides read-only access to the approved Epic sandbox record.</EmptyState>
        )}
      </div>
    </BetaShell>
  );
}
