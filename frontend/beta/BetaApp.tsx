import { useState } from "react";
import { Icons } from "./icons";
import { AgentPage } from "./AgentPage";
import { BetaShell } from "./BetaShell";
import { OverviewPage } from "./OverviewPage";
import { EvidencePage } from "./EvidencePage";
import { Badge, EmptyState, SignIn } from "./ui";
import { PatientsPage } from "./PatientsPage";
import { useBeta } from "./state";
import { BETA_ROUTES, type BetaRouteName } from "./routing";

function UnavailablePage({ route }: { route: BetaRouteName }) {
  const item = BETA_ROUTES.find((r) => r.name === route);
  return (
    <div className="stack" style={{ gap: "var(--space-4)" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">{item?.label}</h1>
          <p className="muted">{item?.description}</p>
        </div>
        <Badge tone="warning" dot>Not available in Beta yet</Badge>
      </div>
      <EmptyState title="Not available in this MOCK slice">
        This page is part of the Beta navigation but has no Beta data source yet. It does not fall back to the Stable demo workspace.
      </EmptyState>
    </div>
  );
}

export function BetaApp() {
  const { route, signedOut, signOut } = useBeta();
  const [signOutError, setSignOutError] = useState(false);
  const controls = signOut && !signedOut && (
    <button type="button" className="btn sm" onClick={() => {
      setSignOutError(false);
      signOut().catch(() => setSignOutError(true));
    }}>Sign out</button>
  );

  return (
    <BetaShell route={route} controls={controls}>
      {signOutError && <div className="banner warning" role="status"><Icons.alert size={16} /><div>Sign-out failed; the session may still be active.</div></div>}
      {signedOut ? <SignIn signedOut /> : route === "patients" ? <PatientsPage /> : route === "agent" ? <AgentPage /> : route === "overview" ? <OverviewPage /> : route === "evidence" ? <EvidencePage /> : <UnavailablePage route={route} />}
    </BetaShell>
  );
}
