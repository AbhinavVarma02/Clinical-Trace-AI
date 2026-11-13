import { useEffect, useState, type ReactNode } from "react";
import { Icons } from "./icons";
import { BETA_ROUTES, navigateBeta, type BetaRouteName } from "./routing";
import { Badge } from "./ui";

export function BetaShell({ children, controls, route, owner = false, onNavigate }: {
  children: ReactNode;
  controls?: ReactNode;
  route: BetaRouteName;
  owner?: boolean;
  onNavigate?: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const current = BETA_ROUTES.find((item) => item.name === route);

  useEffect(() => setMenuOpen(false), [route]);
  useEffect(() => {
    document.title = `${current?.label ?? "Workspace"} · Clinical-Trace AI Beta`;
  }, [current]);

  return (
    <div className="shell">
      <a href="#main" className="sr-only" onClick={(event) => {
        event.preventDefault();
        document.getElementById("main")?.focus();
      }}>Skip to content</a>
      <aside className="sidebar" data-open={menuOpen} aria-label="Primary">
        <div className="brand">
          <div>
            <div className="brand-name">Clinical-Trace AI</div>
            <div className="brand-sub">{owner ? "Epic Beta · local owner" : "Epic Beta · offline synthetic"}</div>
          </div>
        </div>
        <nav className="nav">
          <div className="nav-label">Workspace</div>
          {BETA_ROUTES.map((item) => {
            const Icon = Icons[item.name];
            return (
              <button
                key={item.name}
                type="button"
                className="nav-item"
                aria-current={route === item.name ? "page" : undefined}
                onClick={() => {
                  setMenuOpen(false);
                  if (route !== item.name) onNavigate?.();
                  navigateBeta(item.name);
                }}
                title={owner ? undefined : item.description}
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
          <Badge tone="warning" dot>{owner ? "Epic sandbox" : "Synthetic data"}</Badge>
          {controls}
        </header>
        <main id="main" className="content" tabIndex={-1}>
          <div className="banner warning">
            <Icons.alert size={16} />
            {owner ? (
              <div><strong>Epic Beta.</strong> Approved Epic sandbox synthetic data only. Clinical AI and publication are disabled.</div>
            ) : (
              <div><strong>Epic Beta preview.</strong> Offline synthetic data only. Live Epic requests, real-data promotion and publication are disabled.</div>
            )}
          </div>
          {children}
        </main>
      </div>
    </div>
  );
}
