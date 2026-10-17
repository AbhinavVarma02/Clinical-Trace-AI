import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setApiPersona } from "../api";
import { App } from "../App";
import { AnswerCard } from "../components/AnswerCard";
import { LabTrendChart } from "../components/charts";
import { AppProvider } from "../state";
import { makeRun } from "./fixtures";

type Handler = (url: string, init?: RequestInit) => unknown;

function mockFetch(handler: Handler) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = handler(url, init);
    if (body instanceof Response) return body;
    return new Response(JSON.stringify(body ?? {}), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

const STATUS = {
  service: "Clinical-Trace AI",
  version: "2.0.0-dev",
  status: "ready",
  uptime_s: 1,
  database: { engine: "sqlite", schema_version: 4 },
  data: { patients: 4, origin: "synthetic", startup_ingestion: { received: 86, inserted: 86, duplicates: 0, invalid: 0, status: "succeeded" } },
  retrieval: { index_version: "abc", embedder: "lsa", patients: 4, chunks: 117, cache_hits: 0, cache_misses: 0, built_at: 1 },
  readmission_model: { loaded: false, metadata: null },
  inference: { mode: "deterministic", provider: null, label: "Deterministic mode" },
  evaluation: { run_at: "2026-10-17T00:00:00Z", dataset_version: "v1", gate_passed: true, mode: "deterministic" },
  disclaimer: "educational",
};

beforeEach(() => {
  window.localStorage.clear();
  window.location.hash = "";
  setApiPersona("clinician.demo");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("parses fragmented progress and returns only the final verified result", async () => {
    const run = makeRun();
    const wire = new TextEncoder().encode(`event: progress\ndata: ${JSON.stringify({ run_id: run.run_id, node: "verify", duration_ms: 1 })}\n\nevent: result\ndata: ${JSON.stringify(run)}\n\n`);
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      for (let i = 0; i < wire.length; i += 7) controller.enqueue(wire.slice(i, i + 7));
      controller.close();
    } });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(stream, { headers: { "Content-Type": "text/event-stream" } })));
    const progress = vi.fn();
    expect(await api.runAgentStream({ patient_id: run.patient_id, question: run.question }, progress)).toEqual(run);
    expect(progress).toHaveBeenCalledWith({ run_id: run.run_id, node: "verify", duration_ms: 1 });
  });

  it("rejects a stream that ends before its verified result", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('event: progress\ndata: {"run_id":"r","node":"guard","duration_ms":1}\n\n')));
    await expect(api.runAgentStream({ patient_id: "synthetic_p001", question: "metformin history" }, vi.fn())).rejects.toMatchObject({ code: "stream_interrupted" });
  });

  it("sends the persona header and maps structured errors", async () => {
    const fetchMock = mockFetch(() =>
      new Response(JSON.stringify({ error: { code: "forbidden", message: "patient is outside the panel", request_id: "req-1" } }), { status: 403 }),
    );
    setApiPersona("clinician.restricted");
    const error = await api.patient("synthetic_p002").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(403);
    expect((error as ApiError).message).toBe("patient is outside the panel");
    const headers = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    expect(headers.get("X-Demo-Persona")).toBe("clinician.restricted");
  });

  it("reports network failures clearly", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("offline");
    }));
    await expect(api.status()).rejects.toMatchObject({ code: "network_error" });
  });
});

describe("AnswerCard", () => {
  it("renders verified claims with citations and the deterministic mode label", async () => {
    mockFetch(() => ({}));
    render(<AppProvider><AnswerCard run={makeRun()} /></AppProvider>);
    expect(screen.getByText("Answered")).toBeInTheDocument();
    expect(screen.getByText(/Deterministic · no LLM/)).toBeInTheDocument();
    expect(screen.getByText("1/1 claims verified")).toBeInTheDocument();
    const claims = screen.getByRole("list", { name: "Verified claims" });
    expect(within(claims).getByText(/recorded as stopped/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open evidence p001-m1@v2" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("MedicationRequest/p001-m1", { selector: "dd" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows refusals without claims or a review button", () => {
    const run = makeRun({
      outcome: "refused",
      refusal: { category: "clinical_decision", message: "This prototype does not diagnose, prescribe, or recommend treatment." },
      answer: { summary: null, claims: [], rejected_claims: [], uncertainty: [], abstain_reason: null },
      verification: null,
      tool_calls: [],
    });
    render(<AppProvider><AnswerCard run={run} /></AppProvider>);
    expect(screen.getByText(/Not answered \(Clinical Decision\)/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Verified claims" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Submit for human review/ })).not.toBeInTheDocument();
  });

  it("labels live LLM answers and lists claims removed by verification", async () => {
    const run = makeRun({
      inference: {
        mode: "llm",
        planner: "llm",
        composer: "deterministic-template",
        fallback_used: true,
        provider: { provider: "ollama", model: "qwen2.5:7b" },
        label: "Live language-model inference",
        llm_attempt: { status: "failed", claims_total: 1, claims_supported: 0, rejected: [{ text: "HbA1c was 12.5 %.", citations: [], kind: "generated", reasons: ["numbers not found in cited evidence: 12.5"] }] },
      },
      review: { required: true, reasons: ["the LLM answer failed verification and a deterministic answer was used"] },
    });
    render(<AppProvider><AnswerCard run={run} /></AppProvider>);
    expect(screen.getByText(/Live LLM · qwen2.5:7b/)).toBeInTheDocument();
    expect(screen.getByText("Review recommended")).toBeInTheDocument();
    await userEvent.click(screen.getByText(/Claims removed by verification/));
    expect(screen.getByText("HbA1c was 12.5 %.")).toBeInTheDocument();
    expect(screen.getByText(/numbers not found/)).toBeInTheDocument();
  });

  it("submits an answered run for review", async () => {
    const fetchMock = mockFetch((url) =>
      url.endsWith("/reviews") ? { review_id: "rev-abcdef12", status: "pending_review", artifact_version: 1 } : {},
    );
    render(<AppProvider><AnswerCard run={makeRun()} /></AppProvider>);
    await userEvent.click(screen.getByRole("button", { name: "Submit for human review" }));
    expect(await screen.findByText(/Submitted as review rev-abcd/)).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/reviews"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ run_id: "run-1234567890" });
  });
});

describe("LabTrendChart", () => {
  it("exposes the plotted values as an accessible description", () => {
    const point = (value: number, effective: string, id: string) => ({
      value, effective, unit: "%", source: { reference: `Observation/${id}`, version: "1", last_updated: null, synthetic: true },
    });
    render(
      <LabTrendChart series={{ loinc: "4548-4", name: "Hemoglobin A1c", unit: "%", count: 2, direction: "up", change: 2.2,
        points: [point(7.2, "2025-01-08T00:00:00Z", "o1"), point(9.4, "2025-10-15T00:00:00Z", "o16")] }} />,
    );
    expect(screen.getByRole("img").getAttribute("aria-label")).toContain("7.2 % on 2025-01-08; 9.4 % on 2025-10-15");
  });
});

describe("App shell", () => {
  it("discards an old persona's pending agent response", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    window.location.hash = "#/agent";
    mockFetch((url) => {
      if (url.endsWith("/system/status")) return STATUS;
      if (url.endsWith("/personas")) return [
        { persona_id: "clinician.demo", display_name: "Demo Clinician" },
        { persona_id: "admin.demo", display_name: "Demo Administrator" },
      ];
      if (url.endsWith("/patients")) return [];
      return {};
    });
    let resolveRun!: (run: ReturnType<typeof makeRun>) => void;
    vi.spyOn(api, "runAgentStream").mockImplementation(() => new Promise((resolve) => { resolveRun = resolve; }));
    render(<AppProvider><App /></AppProvider>);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "Why was metformin stopped?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Demo persona" }), "admin.demo");
    await act(async () => resolveRun(makeRun()));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Verified claims" })).not.toBeInTheDocument();
  });

  it("renders the overview from live API data and navigates with the sidebar", async () => {
    mockFetch((url) => {
      if (url.endsWith("/system/status")) return STATUS;
      if (url.endsWith("/personas")) return [{ persona_id: "clinician.demo", display_name: "Demo Clinician", role: "clinician", panel: [], description: "" }];
      if (url.includes("/agent/recent")) return [];
      if (url.endsWith("/patients")) return [];
      if (url.endsWith("/evaluation/latest")) return new Response(JSON.stringify({ error: { code: "not_found", message: "no evaluation results" } }), { status: 404 });
      return {};
    });
    render(<AppProvider><App /></AppProvider>);
    expect(await screen.findByText("Passed")).toBeInTheDocument();
    expect(screen.getByText("No runs yet")).toBeInTheDocument();
    expect(screen.getAllByText("Deterministic").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: /Evaluation/ }));
    await act(async () => {
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await waitFor(() => expect(screen.getByRole("heading", { name: "Evaluation" })).toBeInTheDocument());
    expect(await screen.findByText("no evaluation results")).toBeInTheDocument();
  });
});
