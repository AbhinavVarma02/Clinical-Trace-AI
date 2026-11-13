import { Card } from "../workspace/components/ui";
export { Badge, Card, Disclosure, EmptyState, Skeleton, Tabs, type Tone } from "../workspace/components/ui";

export function formatDate(value: string | null | undefined, withTime = false): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, withTime
    ? { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }
    : { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}

export function formatUnix(value: number | null | undefined, withTime = true): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return formatDate(new Date(value * 1000).toISOString(), withTime);
}

/** Sign-in is a top-level form POST (the browser must follow the redirect to the identity provider itself). */
export function SignIn({ expired = false, signedOut = false }: { expired?: boolean; signedOut?: boolean }) {
  return (
    <Card title={signedOut ? "Signed out" : expired ? "Session ended" : "Owner sign-in required"} subtitle="Beta is single-owner; there is no demo persona">
      <form method="post" action="/beta/auth/browser-login" className="stack">
        <p className="muted">
          {expired ? "Your session expired or was revoked. " : ""}Sign in to read the approved MOCK patients. Nothing is shown until the owner session is valid.
        </p>
        <div>
          <button type="submit" className="btn primary">Sign in as owner</button>
        </div>
      </form>
    </Card>
  );
}

export function humanize(value: string): string {
  return value.replace(/[_:]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
