// Shared presentational primitives. All state comes from props; no data fetching here.
import { useEffect, useRef, type ReactNode } from "react";
import { isTransient, type ApiError } from "../api";
import { Icons } from "./icons";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "teal" | "violet" | "outline";

export function Badge({ tone = "neutral", children, dot = false, title }: { tone?: Tone; children: ReactNode; dot?: boolean; title?: string }) {
  return (
    <span className={`badge ${tone === "neutral" ? "" : tone}`} title={title}>
      {dot && <span className="dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function Card({ title, subtitle, actions, children, flush = false, className = "" }: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-header">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="row">{actions}</div>}
        </header>
      )}
      <div className={`card-body ${flush ? "flush" : ""}`}>{children}</div>
    </section>
  );
}

/** Native disclosure: detail stays one click away (keyboard and screen-reader friendly) without crowding the page. */
export function Disclosure({ label, hint, children, className = "" }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <details className={`disclosure ${className}`}>
      <summary>
        <span>{label}</span>
        {hint && <span className="disclosure-hint">{hint}</span>}
      </summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}

export function Stat({ label, value, note, tone }: { label: string; value: ReactNode; note?: ReactNode; tone?: Tone }) {
  return (
    <div className="card stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={tone ? { color: `var(--${tone === "info" ? "accent" : tone})` } : undefined}>
        {value}
      </div>
      {note && <div className="stat-note">{note}</div>}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span className="row muted" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="stack" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton" style={{ height: 14, width: `${92 - i * 14}%` }} />
      ))}
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div>{children}</div>}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry, action }: { error: ApiError; onRetry?: () => void; action?: ReactNode }) {
  if (isTransient(error)) {
    return (
      <div className="error-state transient" role="status" aria-live="polite">
        <span className="spinner" aria-hidden="true" />
        <div className="error-title">{error.code === "backend_waking" ? "Analysis backend is starting" : "Reconnecting to the backend"}</div>
        <div>{error.message}</div>
        <div className="faint" style={{ fontSize: 12 }}>This section reloads automatically when the backend is ready.</div>
        {onRetry && (
          <div className="row" style={{ justifyContent: "center" }}>
            <button className="btn sm" onClick={onRetry}>
              <Icons.refresh size={15} /> Retry now
            </button>
          </div>
        )}
      </div>
    );
  }
  if (error.code === "backend_not_configured") {
    return (
      <div className="error-state transient" role="status">
        <div className="error-title">Backend not connected</div>
        <div>{error.message}</div>
      </div>
    );
  }
  const title =
    error.status === 403 ? "Not permitted for this persona" : error.status === 401 ? "Persona required"
      : error.status === 429 ? (error.code === "daily_limit" ? "Daily free limit reached" : "Please slow down")
      : error.status === 0 ? "Service unreachable" : "Something went wrong";
  return (
    <div className="error-state" role="alert">
      <Icons.alert size={22} />
      <div className="error-title">{title}</div>
      <div>{error.message}</div>
      {error.requestId && <div className="faint mono">request {error.requestId.slice(0, 8)}</div>}
      <div className="row" style={{ justifyContent: "center" }}>
        {onRetry && error.status !== 429 && (
          <button className="btn sm" onClick={onRetry}>
            <Icons.refresh size={15} /> Try again
          </button>
        )}
        {action}
      </div>
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button key={option.value} type="button" aria-pressed={option.value === value} onClick={() => onChange(option.value)}>
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({ value, tabs, onChange, label }: {
  value: T;
  tabs: { value: T; label: string; count?: number }[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button
          key={tab.value}
          role="tab"
          type="button"
          className="tab"
          aria-selected={tab.value === value}
          onClick={() => onChange(tab.value)}
        >
          {tab.label}
          {tab.count !== undefined && <span className="count">{tab.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Drawer({ title, subtitle, onClose, children }: { title: ReactNode; subtitle?: ReactNode; onClose: () => void; children: ReactNode }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      }
      if (event.key === "Tab") {
        const elements = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
        ) ?? []).filter((element) => !element.hidden);
        const first = elements[0];
        const last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside ref={dialogRef} className="drawer" role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : "Details"}>
        <header className="drawer-header">
          <div>
            <h2 className="card-title">{title}</h2>
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
          <button ref={closeRef} className="btn icon ghost" onClick={onClose} aria-label="Close panel">
            <Icons.close />
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}

export function formatDate(value: string | null | undefined, withTime = false): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, withTime
    ? { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }
    : { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}

export function pct(value: number | null | undefined, digits = 1): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(value < 10 ? 1 : 0)} ms`;
}

export const OUTCOME_TONE: Record<string, Tone> = {
  answered: "success",
  abstained: "warning",
  refused: "danger",
  cancelled: "neutral",
  error: "danger",
};

export const REVIEW_TONE: Record<string, Tone> = {
  pending_review: "info",
  approved: "success",
  rejected: "danger",
  needs_revision: "warning",
  superseded: "neutral",
};

export function humanize(value: string): string {
  return value.replace(/[_:]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
