import { useCallback, useEffect, useState } from "react";
import { BetaShell } from "./BetaShell";
import { CandidateSummary } from "./CandidateSummary";
import type { BetaView } from "./contract";
import type { CandidateResult } from "./genuine";
import { describe, liveApprove, liveErase, liveRecord, liveRefresh, liveSession, liveStatus, type LiveSession, type LiveStatus } from "./live";
import { DataOrigin, Provenance, RecordTabs, RefreshStatus, WarningList } from "./PatientRecord";
import { BETA_ROUTES, navigateBeta, useBetaRoute } from "./routing";
import { Card, EmptyState, formatUnix, humanize } from "./ui";

type Busy = "refresh" | "approve" | "erase" | null;

// Hosted, owner-only LIVE Beta. Refresh asks the hosted backend for a fresh Epic sandbox retrieval; the owner reviews
// the validated candidate and approves it by its exact attempt and manifest hash before it is displayed. No clinical AI,
// publication, storage or fixture path exists in this build.
export function LiveBetaApp() {
  const route = useBetaRoute();
  const [session, setSession] = useState<LiveSession | null>(null);
  const [view, setView] = useState<BetaView | null>(null);
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [candidate, setCandidate] = useState<CandidateResult | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async (signal?: AbortSignal) => {
    const [nextSession, nextView, nextStatus] = await Promise.all([liveSession(signal), liveRecord(signal), liveStatus(signal)]);
    setSession(nextSession);
    setView(nextView);
    setStatus(nextStatus);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    reload(controller.signal).then(
      () => setLoaded(true),
      (error) => { if (!controller.signal.aborted) { setNotice(describe(error)); setLoaded(true); } },
    );
    return () => controller.abort();
  }, [reload]);

  const run = async (kind: Exclude<Busy, null>, action: () => Promise<void>) => {
    setBusy(kind);
    setNotice(null);
    try {
      await action();
    } catch (error) {
      setNotice(describe(error));
    } finally {
      setBusy(null);
      try { setStatus(await liveStatus()); } catch { /* the notice already explains */ }
    }
  };

  const refresh = () => run("refresh", async () => {
    setCandidate(await liveRefresh());
  });
  const approve = () => run("approve", async () => {
    if (!candidate) return;
    setView(await liveApprove(candidate.provenance));
    setCandidate(null);
    navigateBeta("patients");
  });
  const erase = () => run("erase", async () => {
    await liveErase();
    setCandidate(null);
    setView(null);
  });

  const recordPage = route === "patients" || route === "evidence";
  const enabled = session?.epic_enabled === true;
  return (
    <BetaShell route={route} live>
      <div className="stack" style={{ gap: "var(--space-4)" }}>
        <div className="page-header">
          <div>
            <h1 className="page-title">{BETA_ROUTES.find((item) => item.name === route)?.label}</h1>
            <p className="muted">Epic Beta · hosted live retrieval (owner only)</p>
          </div>
        </div>

        {notice && (
          <div className="banner warning" role="status">
            <div>{notice}</div>
          </div>
        )}

        {route === "overview" && (
          <Card title="Live Epic sandbox retrieval" subtitle={session ? `Approved patient alias ${session.patient.alias}` : "Connecting to the hosted backend…"}
            actions={
              <button type="button" className="btn" disabled={!enabled || busy !== null} aria-busy={busy === "refresh"} onClick={refresh}
                title={enabled ? "Request a fresh, read-only retrieval from the Epic FHIR R4 sandbox" : "Live retrieval is switched off"}>
                {busy === "refresh" ? "Retrieving from Epic…" : "Fetch from Epic"}
              </button>
            }>
            <div className="stack">
              {view && <div className="row"><DataOrigin view={view} /></div>}
              <p>
                Fetch sends one bounded, read-only request set to Epic (OAuth, then eight FHIR R4 searches for the one approved
                synthetic test patient). The validated result is staged for your review. Nothing is displayed until you approve that
                exact candidate.
              </p>
              {!enabled && session && <p className="muted">Live retrieval is switched off on the hosted backend.</p>}
              {view?.active && <p className="muted">Displayed record retrieved {formatUnix(view.active.retrieved_at)} UTC.</p>}
              {candidate && (
                <>
                  <CandidateSummary candidate={candidate} />
                  <div className="row">
                    <button type="button" className="btn" disabled={busy !== null} aria-busy={busy === "approve"} onClick={approve}>
                      {busy === "approve" ? "Approving…" : "Approve and display this candidate"}
                    </button>
                  </div>
                </>
              )}
              {view && <div><button type="button" className="btn ghost" onClick={() => navigateBeta("patients")}>Open Patients</button></div>}
            </div>
          </Card>
        )}

        {recordPage && !loaded && <p role="status">Loading the live record…</p>}
        {recordPage && loaded && !view && (
          <EmptyState title="No approved retrieval yet">Use Fetch from Epic on the Overview page, review the candidate, then approve it.</EmptyState>
        )}
        {route === "evidence" && view && (
          <Card title="Source evidence">
            <p className="muted">Source coverage and provenance for the approved generation. Clinical answer generation and citations are disabled.</p>
          </Card>
        )}
        {recordPage && view?.record && (
          <>
            <WarningList warnings={view.warnings} />
            {route === "patients" && <RecordTabs key={view.active?.generation_id} record={view.record} view={view} />}
            <RefreshStatus view={view} refresh={{
              start: enabled ? () => { navigateBeta("overview"); void refresh(); } : undefined,
              running: busy === "refresh", notice: null, label: "Fetch from Epic", title: "Request a fresh Epic sandbox retrieval",
            }} />
            <Provenance view={view} />
          </>
        )}

        {route === "operations" && (
          <Card title="Hosted backend" subtitle="Content-free status of the latest Epic attempt">
            <div className="stack">
              {status?.attempt ? (
                <dl className="kv">
                  <dt>Last attempt</dt><dd>{humanize(status.attempt.state)}{status.attempt.error_code ? ` · ${humanize(status.attempt.error_code)}` : ""}</dd>
                  <dt>Requests</dt><dd>{status.attempt.requests ?? 0}</dd>
                  {Object.entries(status.attempt.operations).map(([name, counts]) => (
                    <div key={name} style={{ display: "contents" }}>
                      <dt className="mono">{name}</dt><dd>HTTP {counts.http_statuses?.join(", ") || "—"} · matches {counts.matches ?? "—"}</dd>
                    </div>
                  ))}
                </dl>
              ) : <p className="muted">No Epic attempt yet in this backend session.</p>}
              <p className="muted">Hosted storage is ephemeral: a backend restart erases staged and approved data.</p>
              <div>
                <button type="button" className="btn ghost" disabled={busy !== null} onClick={erase}>
                  {busy === "erase" ? "Erasing…" : "Erase hosted Epic data"}
                </button>
              </div>
            </div>
          </Card>
        )}

        {!recordPage && route !== "overview" && route !== "operations" && (
          <EmptyState title="Workflow disabled in Epic Beta">This milestone provides owner-only live retrieval of the approved Epic sandbox record.</EmptyState>
        )}
      </div>
    </BetaShell>
  );
}
