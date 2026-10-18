import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { ErrorState } from "../components/ui";
import { AvailabilityProvider, executionAllowance, useAvailability, type Availability, type EdgeStatus } from "../edge";
import { AvailabilityBanner, GuidedCase } from "../components/Availability";
import { AgentPage } from "../pages/Agent";
import { AppProvider } from "../state";

function status(): EdgeStatus {
  return { edge: { status: "ok", release: "test", served_at: new Date().toISOString() },
    backend: { state: "ready", latency_ms: 1, checked_at: new Date().toISOString(), inference_mode: "llm" },
    inference: { provider: "test", model: "test", configured: true, daily_neuron_budget: 8000, neurons_used_today: 8000,
      remaining_fraction: 0, quota_state: "exhausted", resets_in_s: 100 },
    limits: { agent_runs_today: 20, agent_runs_global_per_day: 300, agent_runs_per_visitor_per_day: 25,
      remaining_agent_runs_today: 280, remaining_visitor_agent_runs_today: 0,
      resets_at: new Date(Date.now() + 60_000).toISOString() } };
}
function availability(s: EdgeStatus): Availability {
  return { mode: "edge", backend: "ready", status: s, waitingSince: null, readyEpoch: 0, checkNow() {} };
}
function Check() { const a = useAvailability(); return <><output>{a.mode}/{a.backend}</output><button onClick={a.checkNow}>Refresh status</button></>; }

beforeEach(() => { localStorage.clear(); Element.prototype.scrollIntoView = vi.fn(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("execution allowance", () => {
  it.each(["rate_limited", "daily_limit"])("restores an allowed section's manual retry after %s expires", async code => {
    vi.useFakeTimers();
    const retry = vi.fn();
    const error = new ApiError(429, code, "Limit reached", undefined,
      { retryAfterS: 1, resetsAt: new Date(Date.now() + 1000).toISOString() });
    render(<ErrorState error={error} onRetry={retry} />);
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(1010); });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledOnce();
  });
  it("never presents global allowance as a visitor's when running against an old Worker", () => {
    const s = status(); delete s.limits.remaining_visitor_agent_runs_today;
    const a = executionAllowance(availability(s));
    expect(a.visitor).toBeNull(); expect(a.global).toBe(280); expect(a.blocked).toBe(false);
  });

  it("handles visitor exhaustion, deployment exhaustion, unknown accounting and midnight", () => {
    const s = status(); const a = availability(s);
    expect(executionAllowance(a).reason).toContain("connection's daily");
    s.limits.remaining_visitor_agent_runs_today = 10; s.limits.remaining_agent_runs_today = 0;
    expect(executionAllowance(a).reason).toContain("deployment-wide");
    expect(executionAllowance(a, Date.parse(s.limits.resets_at!)).blocked).toBe(false);
    s.limits.remaining_agent_runs_today = null; s.limits.remaining_visitor_agent_runs_today = null;
    expect(executionAllowance(a).visitor).toBeNull(); expect(executionAllowance(a).blocked).toBe(false);
  });

  it("does not block deterministic answers merely because inference quota is exhausted", () => {
    const s = status(); s.limits.remaining_visitor_agent_runs_today = 10;
    expect(executionAllowance(availability(s)).blocked).toBe(false);
  });

  it("disables execution while keeping patient browsing and recovers after an authoritative refresh", async () => {
    let s = status();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(url === "/edge/status" ? s : []), { headers: { "content-type": "application/json" } })));
    render(<AvailabilityProvider><AppProvider><AvailabilityBanner /><GuidedCase /><AgentPage /><Check /></AppProvider></AvailabilityProvider>);
    await waitFor(() => expect(screen.getByRole("button", { name: "Try a guided case" })).toBeDisabled());
    fireEvent.change(screen.getByRole("textbox", { name: "Question" }), { target: { value: "Summarize records" } });
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Synthetic patient" })).toBeEnabled();
    expect(screen.getAllByText(/connection's daily agent-run allowance is used up/).length).toBeGreaterThan(0);
    // Daily exhaustion takes precedence over the live-AI fallback banner.
    expect(screen.queryByText(/Answers continue in deterministic/)).not.toBeInTheDocument();
    s = status(); s.limits.remaining_visitor_agent_runs_today = 25;
    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled());
  });

  it.each(["daily_limit", "rate_limited"])("retains a scoped %s from SSE start despite stale status and expires without retrying execution", async code => {
    const s = status(); s.limits.remaining_visitor_agent_runs_today = 10;
    let runs = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/stream")) {
        runs++;
        return new Response(JSON.stringify({ error: { code, message: "Limit reached", scope: "visitor", limit_kind: "agent_runs", retry_after_s: 1,
          resets_at: new Date(Date.now() + 1000).toISOString() } }), { status: 429, headers: { "content-type": "application/json", "retry-after": "1" } });
      }
      return new Response(JSON.stringify(url === "/edge/status" ? s : []), { headers: { "content-type": "application/json" } });
    }));
    render(<AvailabilityProvider><AppProvider><AgentPage /></AppProvider></AvailabilityProvider>);
    await act(async () => { await Promise.resolve(); });
    fireEvent.change(screen.getByRole("textbox", { name: "Question" }), { target: { value: "Summarize records" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled());
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled(), { timeout: 3000 });
    expect(runs).toBe(1);
  });

  it("keeps a known edge in unavailable state after HTTP failure and recovers without assuming direct mode", async () => {
    let fail = false;
    vi.stubGlobal("fetch", vi.fn(async () => fail ? new Response("unavailable", { status: 503 })
      : new Response(JSON.stringify(status()), { headers: { "content-type": "application/json" } })));
    render(<AvailabilityProvider><AvailabilityBanner /><Check /></AvailabilityProvider>);
    await screen.findByText(/connection's daily agent-run allowance/);
    fail = true; fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    // The saved allowance survives an outage rather than re-enabling execution.
    await screen.findByText("edge/unavailable");
    expect(screen.getByText(/connection's daily agent-run allowance/)).toBeInTheDocument();
    fail = false; fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByText("edge/ready");
  });

  it("preserves scope and reset metadata for ordinary API errors too", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "daily_limit", message: "limit", scope: "deployment", resets_at: "2026-10-19T00:00:00Z" } }), { status: 429, headers: { "content-type": "application/json", "retry-after": "20" } })));
    await expect(api.runAgent({ patient_id: "synthetic_p001", question: "q" })).rejects.toMatchObject({ code: "daily_limit", limit: { scope: "deployment", retryAfterS: 20, resetsAt: "2026-10-19T00:00:00Z" } });
  });
});
