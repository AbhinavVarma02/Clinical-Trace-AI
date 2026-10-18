// Application frame: sidebar navigation, top bar (persona, theme, inference mode), safety notice.
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { ROUTES, navigate, useApi, useApp, type RouteName } from "../state";
import { useAvailability } from "../edge";
import { AvailabilityBanner, AvailabilityPill, GuideBar } from "./Availability";
import { Icons } from "./icons";
import { Badge } from "./ui";

const NAV_ICONS: Record<RouteName, (p: { size?: number }) => ReactNode> = {
  overview: Icons.overview,
  patients: Icons.patients,
  agent: Icons.agent,
  risk: Icons.risk,
  evidence: Icons.evidence,
  evaluation: Icons.evaluation,
  operations: Icons.operations,
  governance: Icons.governance,
};

// Shown until the backend answers, so the selector is readable during cold starts.
const FALLBACK_PERSONAS = [
  { persona_id: "clinician.demo", display_name: "Demo Clinician" },
  { persona_id: "clinician.restricted", display_name: "Restricted Clinician" },
  { persona_id: "reviewer.demo", display_name: "Demo Reviewer" },
  { persona_id: "admin.demo", display_name: "Demo Administrator" },
];

export function Shell({ children }: { children: ReactNode }) {
  const { route, persona, setPersona, theme, toggleTheme } = useApp();
  const [menuOpen, setMenuOpen] = useState(false);
  const personas = useApi(api.personas, "personas");
  const status = useApi(api.status, "status");
  const current = ROUTES.find((r) => r.name === route.name);

  useEffect(() => setMenuOpen(false), [route.name]);
  useEffect(() => {
    document.title = `${current?.label ?? "Workspace"} · Clinical-Trace AI`;
  }, [current]);

  const mode = status.data?.inference.mode;
  const availability = useAvailability();
  return (
    <div className="shell">
      <a href="#main" className="sr-only">Skip to content</a>
      <aside className="sidebar" data-open={menuOpen} aria-label="Primary">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            {/* A record trace that ends in a cited point: the product in one glyph. */}
            <svg viewBox="0 0 32 32" focusable="false">
              <rect width="32" height="32" rx="8" fill="currentColor" />
              <path d="M6.5 17.5h4l2.5-6.5 4 12 2.75-8.25 1.5 2.75h3.25" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              <circle cx="25.5" cy="17.5" r="1.9" fill="#fff" />
            </svg>
          </span>
          <div>
            <div className="brand-name">Clinical-Trace AI</div>
            <div className="brand-sub">Evidence-grounded record review</div>
          </div>
        </div>
        <nav className="nav">
          <div className="nav-label">Workspace</div>
          {ROUTES.map((item) => {
            const Icon = NAV_ICONS[item.name];
            return (
              <button
                key={item.name}
                type="button"
                className="nav-item"
                aria-current={route.name === item.name ? "page" : undefined}
                onClick={() => navigate(item.name)}
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
          <div className="topbar-title">
            <span className="topbar-crumb hide-mobile">Workspace</span>
            <span className="topbar-sep hide-mobile" aria-hidden="true">/</span>
            <span className="topbar-page">{current?.label}</span>
          </div>
          <div className="topbar-spacer" />
          {/* On phones the availability banner carries the same state; keep the bar uncluttered. */}
          <span className="hide-mobile"><AvailabilityPill /></span>
          {mode && availability.mode !== "edge" && (
            <span className="hide-mobile">
              {mode === "llm" ? (
                <Badge tone="violet" dot title={status.data?.inference.label}>Live LLM · {status.data?.inference.provider?.model}</Badge>
              ) : (
                <Badge tone="teal" dot title={status.data?.inference.label}>Deterministic mode</Badge>
              )}
            </span>
          )}
          <label className="sr-only" htmlFor="persona-select">Demo persona</label>
          <select
            id="persona-select"
            className="select persona-select"
            style={{ width: "auto", maxWidth: 200 }}
            value={persona}
            onChange={(event) => setPersona(event.target.value)}
            title="Synthetic access context used for every request"
          >
            {(personas.data ?? FALLBACK_PERSONAS).map((p) => (
              <option key={p.persona_id} value={p.persona_id}>
                {p.display_name}
              </option>
            ))}
          </select>
          <button type="button" className="btn icon" onClick={toggleTheme} aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}>
            {theme === "light" ? <Icons.moon /> : <Icons.sun />}
          </button>
        </header>
        <main id="main" className="content" tabIndex={-1}>
          <AvailabilityBanner />
          <GuideBar />
          {children}
        </main>
      </div>
    </div>
  );
}
