// Beta shell: the eight navigation pages in the familiar layout, built from Beta's own copies of the
// layout styles, icons and primitives (no Stable imports). Only Patients (and the Evidence placeholder)
// have content in this slice.
import { useEffect, useState, type ReactNode } from "react";
import { Icons } from "./icons";
import { Badge, EmptyState } from "./ui";
import { EvidencePlaceholder, PatientsPage } from "./PatientsPage";
import { BETA_ROUTES, navigateBeta, useBeta, type BetaRouteName } from "./state";

const NAV_ICONS: Record<BetaRouteName, (p: { size?: number }) => ReactNode> = {
  overview: Icons.overview,
  patients: Icons.patients,
  agent: Icons.agent,
  risk: Icons.risk,
  evidence: Icons.evidence,
  evaluation: Icons.evaluation,
  operations: Icons.operations,
  governance: Icons.governance,
};

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
      {route === "evidence" && <EvidencePlaceholder />}
    </div>
  );
}

export function BetaApp() {
  const { route } = useBeta();
  const [menuOpen, setMenuOpen] = useState(false);
  const current = BETA_ROUTES.find((r) => r.name === route);

  useEffect(() => setMenuOpen(false), [route]);
  useEffect(() => {
    document.title = `${current?.label ?? "Workspace"} · Clinical-Trace AI Beta`;
  }, [current]);

  return (
    <div className="shell">
      <a href="#main" className="sr-only">Skip to content</a>
      <aside className="sidebar" data-open={menuOpen} aria-label="Primary">
        <div className="brand">
          <div>
            <div className="brand-name">Clinical-Trace AI</div>
            <div className="brand-sub">Epic Beta · MOCK</div>
          </div>
        </div>
        <nav className="nav">
          <div className="nav-label">Workspace</div>
          {BETA_ROUTES.map((item) => {
            const Icon = NAV_ICONS[item.name];
            return (
              <button
                key={item.name}
                type="button"
                className="nav-item"
                aria-current={route === item.name ? "page" : undefined}
                onClick={() => navigateBeta(item.name)}
                title={item.description}
              >
                <Icon size={18} />
                {item.label}
              </button>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          <Icons.alert size={14} />
          <div><strong>Synthetic data only.</strong> Not clinically validated. Not for medical use.</div>
        </div>
      </aside>
      {menuOpen && <div className="drawer-backdrop" style={{ zIndex: 45 }} onClick={() => setMenuOpen(false)} />}
      <div className="main">
        <header className="topbar">
          <button type="button" className="btn icon ghost menu-button" onClick={() => setMenuOpen(true)} aria-label="Open navigation">
            <Icons.menu />
          </button>
          <div className="topbar-title"><span className="topbar-page">{current?.label}</span></div>
          <div className="topbar-spacer" />
          <Badge tone="warning" dot title="Fixtures only; sign-in, live Epic access and publication are disabled">MOCK fixtures</Badge>
        </header>
        <main id="main" className="content" tabIndex={-1}>
          <div className="banner warning">
            <Icons.alert size={16} />
            <div><strong>Epic Beta preview.</strong> Synthetic fixtures only. Real sign-in, live Epic requests and publication are disabled.</div>
          </div>
          {route === "patients" ? <PatientsPage /> : <UnavailablePage route={route} />}
        </main>
      </div>
    </div>
  );
}
