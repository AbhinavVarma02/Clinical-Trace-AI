import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, getWorkspaceId, isTransient, resetWorkspaceId } from "../api";
import { formatDuration, isEdgeStatus } from "../edge";
import { GUIDE_STEPS, markGuideStep, reloadGuide, resetGuide } from "../guide";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("public demo client", () => {
  it("sends a stable random workspace id with every request", async () => {
    const seen: Headers[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      seen.push(new Headers(init?.headers));
      return new Response("[]", { headers: { "Content-Type": "application/json" } });
    }));
    await api.patients();
    await api.reviews();
    const id = seen[0]!.get("X-Demo-Workspace");
    expect(id).toMatch(/^[a-f0-9]{32}$/);
    expect(seen[1]!.get("X-Demo-Workspace")).toBe(id);
    expect(getWorkspaceId()).toBe(id);
    expect(resetWorkspaceId()).not.toBe(id);
  });

  it("classifies edge cold-start errors as transient", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "backend_waking", message: "starting" } }),
      { status: 503, headers: { "Content-Type": "application/json" } })));
    const error = await api.patients().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(isTransient(error as ApiError)).toBe(true);
    expect(isTransient(new ApiError(403, "forbidden", "no"))).toBe(false);
  });

  it("recognises only genuine edge status payloads", () => {
    expect(isEdgeStatus({})).toBe(false);
    expect(isEdgeStatus({ edge: { status: "ok" }, backend: { state: "ready" }, inference: {}, limits: {} })).toBe(true);
    expect(formatDuration(42)).toBe("42 s");
    expect(formatDuration(3700)).toBe("1 h 1 min");
  });

  it("records guided-case steps only when marked", () => {
    resetGuide();
    markGuideStep("run", "run-1");
    reloadGuide();
    const stored = JSON.parse(window.localStorage.getItem("ct-guide")!);
    expect(stored.done).toEqual(["run"]);
    expect(stored.runId).toBe("run-1");
    expect(GUIDE_STEPS.map((s) => s.id)).toEqual(["run", "citation", "submit", "approve"]);
  });
});
