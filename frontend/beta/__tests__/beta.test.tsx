import { act, render, screen, waitFor, within } from "@testing-library/react";
// @ts-expect-error Node's built-in crypto is test-only; the browser tsconfig intentionally omits Node declarations.
import { webcrypto } from "node:crypto";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BetaApp } from "../BetaApp";
import { BetaApiError, fixtureClient, httpClient, UnknownPatientError, type BetaClient } from "../client";
import type { Answer, BetaView } from "../contract";
import { VIEWS } from "../fixtures";
import { BETA_ROUTES, BetaProvider, parseRoute } from "../state";

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchSpy = vi.fn(() => Promise.reject(new Error("network is disabled in Beta MOCK tests")));
  vi.stubGlobal("fetch", fetchSpy);
  vi.stubGlobal("crypto", webcrypto);
  window.location.hash = "";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderBeta(hash = "#/patients", client?: BetaClient, pollMs?: number) {
  window.location.hash = hash;
  return render(
    <BetaProvider client={client} pollMs={pollMs}>
      <BetaApp />
    </BetaProvider>,
  );
}

describe("Beta navigation", () => {
  it("keeps exactly the eight existing page names and labels (hard-coded, not imported from Stable)", () => {
    expect(BETA_ROUTES.map((r) => [r.name, r.label])).toEqual([
      ["overview", "Overview"],
      ["patients", "Patients"],
      ["agent", "Case Review Agent"],
      ["risk", "Risk & Explainability"],
      ["evidence", "Evidence Explorer"],
      ["evaluation", "Evaluation"],
      ["operations", "Operations"],
      ["governance", "Governance"],
    ]);
  });

  it("renders all eight pages in the sidebar", () => {
    renderBeta();
    const nav = screen.getByRole("navigation");
    expect(within(nav).getAllByRole("button")).toHaveLength(8);
  });

  it("reads only the route name from the hash", () => {
    expect(parseRoute("#/patients?patient=mock-alpha")).toBe("patients");
    expect(parseRoute("#/unknown")).toBe("patients");
  });

  it("shows a truthful unavailable page, not Stable demo content, for pages without Beta data", () => {
    renderBeta("#/risk");
    expect(screen.getByText("Not available in this MOCK slice")).toBeInTheDocument();
    expect(screen.queryByLabelText(/persona/i)).not.toBeInTheDocument();
  });
});

describe("Beta patients page (fixture client)", () => {
  it("asks for a selection before showing any record", () => {
    renderBeta();
    expect(screen.getByText("Select a patient")).toBeInTheDocument();
    expect(screen.queryByText("Clinical record")).not.toBeInTheDocument();
  });

  it("shows only the selected patient's record and never carries values across patients", async () => {
    const user = userEvent.setup();
    renderBeta();

    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await user.click(await screen.findByRole("tab", { name: /Labs/ }));
    expect(await screen.findByText("1.2 mg/dL")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /MOCK Bravo/ }));
    await user.click(await screen.findByRole("tab", { name: /Labs/ }));
    expect(await screen.findByText("4.8 mg/dL")).toBeInTheDocument();
    expect(screen.queryByText("1.2 mg/dL")).not.toBeInTheDocument();
  });

  it("reports coverage truthfully, including sources that were not imported", async () => {
    const user = userEvent.setup();
    renderBeta();
    await user.click(await screen.findByRole("button", { name: /MOCK Bravo/ }));

    const coverage = await screen.findByRole("table", { name: "Source coverage" });
    const row = (source: string) => within(coverage).getByText(source).closest("tr") as HTMLElement;
    expect(within(row("MedicationRequest")).getByText("Not imported")).toBeInTheDocument();
    expect(within(row("DocumentReference.metadata")).getByText("Returned none")).toBeInTheDocument();
    expect(screen.getByText(/Note bodies are never imported/)).toBeInTheDocument();
    expect(screen.getByText(/Medications are not imported/)).toBeInTheDocument();
    expect(screen.getByText("Serving: Fresh")).toBeInTheDocument();
  });

  it("keeps fixture refresh disabled and points to returned-answer evidence without inventing citations", async () => {
    const user = userEvent.setup();
    renderBeta();
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));

    expect(await screen.findByRole("button", { name: /Refresh unavailable/ })).toBeDisabled();
    expect(screen.getByText("Evidence is available only for a returned answer")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Evidence Explorer" })).toHaveAttribute("href", "#/evidence");
    expect(screen.getByText("No review decision is available on this page")).toBeInTheDocument();
  });

  it("makes no network request", async () => {
    const user = userEvent.setup();
    renderBeta();
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await screen.findByText("Clinical record");
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("Stable/Beta isolation", () => {
  it("reuses only Stable presentation modules and does not import Stable or Streamlit workflows", () => {
    const sources = import.meta.glob(["../*.ts", "../*.tsx"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    const presentation = new Map([
      ["../icons.tsx", "../workspace/components/icons"],
      ["../ui.tsx", "../workspace/components/ui"],
    ]);
    expect(Object.keys(sources).length).toBeGreaterThanOrEqual(8);
    for (const [file, text] of Object.entries(sources)) {
      const specifiers = [...text.matchAll(/(?:from|import)\s*\(?\s*["']([^"']+)["']/g)].map((m) => m[1] ?? "");
      for (const spec of specifiers) {
        const relative = spec.startsWith(".");
        expect(!relative || spec.startsWith("./") || presentation.get(file) === spec, `${file} imports ${spec}`).toBe(true);
      }
    }
  });

  it("neither reads nor writes browser storage, so Stable's persona and patient never reach Beta", async () => {
    localStorage.setItem("ct-persona", "admin.demo");
    localStorage.setItem("ct-patient", "synthetic_p001");
    const get = vi.spyOn(Storage.prototype, "getItem");
    const set = vi.spyOn(Storage.prototype, "setItem");
    const user = userEvent.setup();
    renderBeta();
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await screen.findByText("Clinical record");
    expect(get).not.toHaveBeenCalled();
    expect(set).not.toHaveBeenCalled();
    expect(document.cookie).toBe("");
    localStorage.clear();
  });
});

describe("fixture client", () => {
  it("rejects unknown aliases, including prototype member names", async () => {
    for (const alias of ["mock-charlie", "__proto__", "constructor", "toString"]) {
      await expect(fixtureClient.getView(alias)).rejects.toBeInstanceOf(UnknownPatientError);
    }
  });

  it("returns copies so callers cannot mutate the fixtures", async () => {
    const first = await fixtureClient.getView("mock-alpha");
    first.record!.labs.items[0]!.value = 99;
    expect((await fixtureClient.getView("mock-alpha")).record!.labs.items[0]!.value).toBe(1.2);
  });

  it("serves the same contract shape as the server for both patients, covering all eight operation profiles", async () => {
    for (const alias of Object.keys(VIEWS)) {
      const view = await fixtureClient.getView(alias);
      expect(view.mode).toBe("MOCK");
      expect(Object.keys(view.coverage!.operations)).toEqual(
        expect.arrayContaining([
          "Patient",
          "Encounter",
          "Condition:problem-list-item",
          "Condition:encounter-diagnosis",
          "Observation:laboratory",
          "Observation:vital-signs",
          "DocumentReference:clinical-note",
          "DiagnosticReport",
          "MedicationRequest",
          "Binary",
        ]),
      );
    }
  });
});

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

/** A server-shaped view: richer than the fixtures (a report, a warning, a truncated section). */
function serverView(overrides: Partial<BetaView> = {}): BetaView {
  const base = structuredClone(VIEWS["mock-alpha"]!);
  base.data_origin = "synthetic_mock_transport";
  base.record!.reports = {
    items: [{ name: "Cardiac stress test", effective: "2026-11-01T12:00:00Z", conclusion: "No ischemia.", result_count: 2, source: base.record!.encounters.items[0]!.source }],
    total: 1,
    truncated: false,
  };
  base.record!.labs = { ...base.record!.labs, total: 600, truncated: true };
  base.warnings.push({ code: "unresolved_references", severity: "warning", message: "1 reference(s) point at resources that were not imported." });
  return { ...base, ...overrides };
}

describe("HTTP client (same-origin reads of the Beta browser contract)", () => {
  it("sends exactly the request profile the server requires, with no body, token, cookie or storage access", async () => {
    fetchSpy.mockImplementation(() => reply({ patients: [{ alias: "mock-alpha", label: "MOCK Alpha" }] }));
    expect(await httpClient.listPatients()).toEqual([{ alias: "mock-alpha", label: "MOCK Alpha" }]);
    fetchSpy.mockImplementation(() => reply(serverView()));
    await httpClient.getView("mock-alpha");

    expect(fetchSpy.mock.calls.map((call) => call[0])).toEqual(["/beta/epic/patients", "/beta/epic/patients/mock-alpha/record"]);
    for (const [, init] of fetchSpy.mock.calls as [string, RequestInit][]) {
      expect(init).toMatchObject({ method: "GET", mode: "same-origin", credentials: "same-origin", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer" });
      expect(init.headers).toEqual({ "X-Beta-Client": "browser-v1", Accept: "application/json" });
      expect(init.body).toBeUndefined();
    }
    expect(document.cookie).toBe("");
  });

  it("never lets a malformed alias reach the network", async () => {
    for (const alias of ["../auth/session", "Mock-Alpha", "a/b", "a?b=1", "", "x".repeat(33), "mock alpha"]) {
      await expect(httpClient.getView(alias)).rejects.toBeInstanceOf(UnknownPatientError);
    }
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("surfaces only a content-free error code", async () => {
    fetchSpy.mockImplementation(() => reply({ error: { code: "owner_required" } }, 401));
    await expect(httpClient.getView("mock-alpha")).rejects.toMatchObject({ status: 401, code: "owner_required" });
    fetchSpy.mockImplementation(() => reply({ error: { code: "<img onerror=alert(1)>" } }, 500));
    const failure = await httpClient.getView("mock-alpha").catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(BetaApiError);
    expect((failure as BetaApiError).code).toBe("unavailable");
    fetchSpy.mockImplementation(() => Promise.resolve(new Response("<html>proxy error</html>", { status: 502 })));
    await expect(httpClient.getView("mock-alpha")).rejects.toMatchObject({ status: 502, code: "unavailable" });
  });
});

describe("Beta patients page (HTTP client)", () => {
  function serve(view: BetaView) {
    fetchSpy.mockImplementation((path: string) =>
      path === "/beta/auth/session" ? reply({ authenticated: true, mode: "MOCK", csrf_token: "s".repeat(43), server_time_unix_s: 1000, idle_expires_at_unix_s: 1900, absolute_expires_at_unix_s: 2000 }) :
        path === "/beta/epic/patients" ? reply({ patients: [{ alias: "mock-alpha", label: "MOCK Alpha" }] }) : reply(view),
    );
  }

  async function open(view: BetaView) {
    serve(view);
    const user = userEvent.setup();
    const rendered = renderBeta("#/patients", httpClient);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    return { user, ...rendered };
  }

  it("renders every section, the warnings, coverage and provenance from the server view", async () => {
    const { user } = await open(serverView());

    expect(await screen.findByText("Clinical record")).toBeInTheDocument();
    expect(screen.getByLabelText("Completeness warnings")).toHaveTextContent("point at resources that were not imported");
    expect(screen.getAllByText("Synthetic MOCK data").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("tab", { name: /Labs/ }));
    expect(screen.getByText("1.2 mg/dL")).toBeInTheDocument();
    expect(screen.getByText("Showing the first 1 of 600.")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Vital signs/ }));
    expect(screen.getByText("88 /min")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Reports/ }));
    expect(screen.getByText(/Cardiac stress test: No ischemia\./)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Note metadata/ }));
    expect(screen.getByText("Discharge summary")).toBeInTheDocument();

    const operations = screen.getByRole("table", { name: "Per-operation counts" });
    expect(within(operations).getByText("MedicationRequest")).toBeInTheDocument();
    expect(within(operations).getByText("DocumentReference:clinical-note").closest("tr")).toHaveTextContent("Not Imported");
    expect(screen.getByText("Provenance")).toBeInTheDocument();
    expect(screen.getByText("Synthetic MOCK transport; no Epic request")).toBeInTheDocument();
    for (const [path] of fetchSpy.mock.calls as [string][]) expect(path).toMatch(/^\/beta\/(auth\/session|epic\/patients(\/mock-alpha\/record)?)$/);
  });

  it("renders server text as text, never as markup", async () => {
    const view = serverView();
    view.record!.conditions.items[0]!.display = "<img src=x onerror=alert(1)> <b>bold</b>";
    const { container } = await open(view);
    expect(await screen.findByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("main b")).toBeNull();
  });

  it("says plainly when there is no verified generation, and shows no record", async () => {
    await open(
      serverView({
        serving: "unavailable",
        active: null,
        attempt: null,
        coverage: null,
        provenance: null,
        record: null,
        warnings: [{ code: "synthetic_mock", severity: "info", message: "Synthetic MOCK data." }],
      }),
    );
    expect(await screen.findByText("No verified record yet")).toBeInTheDocument();
    expect(screen.getByText("No coverage yet")).toBeInTheDocument();
    expect(screen.getByText("Serving: Unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  });

  it("shows an integrity failure as a failure and withholds the record", async () => {
    await open(serverView({ serving: "unavailable", evidence_error: "generation_integrity", coverage: null, provenance: null, record: null }));
    expect(await screen.findByText(/failed its integrity check/)).toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  });

  it("offers owner sign-in as a plain form post when the session is missing or has expired", async () => {
    fetchSpy.mockImplementation(() => reply({ error: { code: "owner_required" } }, 401));
    renderBeta("#/patients", httpClient);
    const button = await screen.findByRole("button", { name: "Sign in as owner" });
    const form = button.closest("form")!;
    expect(form.getAttribute("method")).toBe("post");
    expect(form.getAttribute("action")).toBe("/beta/auth/browser-login");
    expect(screen.queryByText("Clinical record")).not.toBeInTheDocument();
  });

  it("treats a patient the server refuses as unavailable, with no fallback content", async () => {
    fetchSpy.mockImplementation((path: string) =>
      path === "/beta/epic/patients"
        ? reply({ patients: [{ alias: "mock-alpha", label: "MOCK Alpha" }] })
        : reply({ error: { code: "patient_unavailable" } }, 404),
    );
    const user = userEvent.setup();
    renderBeta("#/patients", httpClient);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    expect(await screen.findByText("Patient unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Clinical record")).not.toBeInTheDocument();
  });
});

describe("Browser refresh", () => {
  it("sends one owner POST with the CSRF token and a fresh idempotency key, after reading the session", async () => {
    fetchSpy.mockImplementation((path: string) =>
      path === "/beta/auth/session" ? reply({ csrf_token: "c".repeat(43) }) : Promise.resolve(new Response(JSON.stringify({ attempt_id: "a" }), { status: 202 })),
    );
    await httpClient.refresh!("mock-alpha");
    await httpClient.refresh!("mock-alpha");

    const calls = fetchSpy.mock.calls as [string, RequestInit][];
    expect(calls.map(([path]) => path)).toEqual([
      "/beta/auth/session",
      "/beta/epic/patients/mock-alpha/refresh",
      "/beta/auth/session",
      "/beta/epic/patients/mock-alpha/refresh",
    ]);
    const posts = calls.filter(([, init]) => init.method === "POST").map(([, init]) => init);
    for (const init of posts) {
      expect(init.headers).toMatchObject({ "X-CSRF-Token": "c".repeat(43) });
      expect((init.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/^[A-Za-z0-9._:/-]{12,128}$/); // the server's shape
      expect(init.body).toBeUndefined();
      // No request-level mode or referrer policy: the page's same-origin policy is what lets the browser send its Origin.
      expect(init).not.toHaveProperty("referrerPolicy");
      expect(init).not.toHaveProperty("mode");
    }
    expect((posts[0]!.headers as Record<string, string>)["Idempotency-Key"]).not.toBe((posts[1]!.headers as Record<string, string>)["Idempotency-Key"]);
  });

  it("reports the server's refusal code and retry delay, and never posts for a malformed alias", async () => {
    fetchSpy.mockImplementation((path: string) =>
      path === "/beta/auth/session"
        ? reply({ csrf_token: "c".repeat(43) })
        : Promise.resolve(new Response(JSON.stringify({ error: { code: "quota_exhausted" } }), { status: 429, headers: { "Retry-After": "290" } })),
    );
    await expect(httpClient.refresh!("mock-alpha")).rejects.toMatchObject({ status: 429, code: "quota_exhausted", retryAfter: 290 });
    fetchSpy.mockClear();
    await expect(httpClient.refresh!("../x")).rejects.toBeInstanceOf(UnknownPatientError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  /** A client whose refresh succeeds and whose view moves queued -> running -> succeeded -> a new generation. */
  function refreshingClient(refresh: BetaClient["refresh"]): { client: BetaClient; reads: () => number } {
    let reads = 0;
    const initial = structuredClone(VIEWS["mock-alpha"]!);
    initial.record = null;
    initial.coverage = null;
    initial.provenance = null;
    initial.active = null;
    initial.attempt = null;
    initial.serving = "unavailable";
    const states = ["running", "running", "succeeded"];
    const client: BetaClient = {
      kind: "http",
      listPatients: async () => [{ alias: "mock-alpha", label: "MOCK Alpha" }],
      getView: async () => {
        reads += 1;
        if (reads === 1) return structuredClone(initial);
        const view = structuredClone(VIEWS["mock-alpha"]!);
        view.attempt!.state = states[Math.min(reads - 2, states.length - 1)]!;
        return view;
      },
      refresh,
    };
    return { client, reads: () => reads };
  }

  it("starts a refresh, shows it running, polls until it settles and then shows the new record", async () => {
    const refresh = vi.fn(() => Promise.resolve());
    const { client } = refreshingClient(refresh);
    const user = userEvent.setup();
    renderBeta("#/patients", client, 0);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    expect(await screen.findByText("No verified record yet")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Refresh MOCK data" }));
    expect(refresh).toHaveBeenCalledWith("mock-alpha");
    expect(await screen.findByText("Clinical record")).toBeInTheDocument();
    expect(screen.queryByText("No verified record yet")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Refresh MOCK data" })).toBeEnabled();
  });

  it("disables the button while a refresh runs so it cannot be started twice", async () => {
    let release: () => void = () => undefined;
    const refresh = vi.fn(() => new Promise<void>((resolve) => { release = resolve; }));
    const { client } = refreshingClient(refresh);
    const user = userEvent.setup();
    renderBeta("#/patients", client, 0);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await user.click(await screen.findByRole("button", { name: "Refresh MOCK data" }));
    const busy = await screen.findByRole("button", { name: /Refreshing/ });
    expect(busy).toBeDisabled();
    release();
    await screen.findByText("Clinical record");
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it.each([
    [new BetaApiError(429, "quota_exhausted", 290), /Try again in 5 minutes/],
    [new BetaApiError(429, "quota_exhausted", 45), /Try again in 45 seconds/],
    [new BetaApiError(409, "refresh_busy"), /already running/],
    [new BetaApiError(500, "internal_error"), /could not be started \(internal error\)/],
  ])("explains a refused refresh in plain words (%#)", async (error, message) => {
    const { client } = refreshingClient(() => Promise.reject(error));
    const user = userEvent.setup();
    renderBeta("#/patients", client, 0);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await user.click(await screen.findByRole("button", { name: "Refresh MOCK data" }));
    expect(await screen.findByRole("status")).toHaveTextContent(message);
    expect(screen.getByRole("button", { name: "Refresh MOCK data" })).toBeEnabled();
    expect(screen.queryByText("Clinical record")).toBeInTheDocument(); // the empty record card, not a fabricated record
    expect(screen.getByText("No verified record yet")).toBeInTheDocument();
  });

  it("withholds the record immediately when refresh discovers expired owner access", async () => {
    const client = askingClient(vi.fn(), { refresh: async () => { throw new BetaApiError(401, "owner_required"); } });
    const user = userEvent.setup();
    renderBeta("#/patients", client);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await screen.findByText("Clinical record");
    await user.click(screen.getByRole("button", { name: "Refresh MOCK data" }));
    expect(await screen.findByText("Session ended")).toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.queryByText("Provenance")).not.toBeInTheDocument();
  });
});

// -- owner actions: ask, revoke, sign out ---------------------------------------------------------------------

const GID = "a".repeat(32);

function sampleAnswer(overrides: Partial<Answer> = {}): Answer {
  return {
    run_id: "run-1",
    question: "What is the latest creatinine?",
    outcome: "answered",
    answer: {
      summary: "2 verified statement(s) drawn from 2 source record(s).",
      claims: [
        { text: "Creatinine 1.0 mg/dL on 2026-10-12.", citations: ["obs-1@v1"], kind: "quote" },
        {
          text: "Creatinine changed by 0.1.",
          citations: ["obs-1@v1", "obs-2@v1"],
          kind: "lab_trend",
          computations: [{ op: "difference", args: ["1", "1.1"], result: "0.1" }],
        },
      ],
      uncertainty: ["Medication orders were not imported."],
      abstain_reason: null,
    },
    refusal: null,
    evidence: [
      { evidence_id: "obs-1@v1", source_ref: "Observation/obs-1", source_version: "1", title: "Creatinine - Result", text: "Creatinine 1.0 mg/dL on 2026-10-12.", date: "2026-10-12T16:00:00Z", kind: "retrieved", cited: true, flags: [] },
      { evidence_id: "obs-2@v1", source_ref: "Observation/obs-2", source_version: "1", title: "Creatinine - Result", text: "Creatinine 1.1 mg/dL on 2026-10-15.", date: "2026-10-15T06:00:00Z", kind: "retrieved", cited: true, flags: [] },
      { evidence_id: "enc-1@v1", source_ref: "Encounter/enc-1", source_version: "1", title: null, text: "Inpatient stay.", date: null, kind: "retrieved", cited: false, flags: [] },
    ],
    verification: { status: "verified", claims_total: 2, claims_supported: 2, isolation_violations: 0 },
    review: { required: true, reasons: ["independent_reviewer_required"] },
    inference: { mode: "deterministic", label: "Deterministic mode: no language model was called." },
    disclaimer: "This is decision-support only and not medical advice.",
    serving: "fresh",
    error: null,
    ...overrides,
  };
}

/** A client over the fixture views with an active generation id the server would accept. */
function askingClient(ask: BetaClient["ask"], extra: Partial<BetaClient> = {}, serving: BetaView["serving"] = "fresh"): BetaClient {
  return {
    kind: "http",
    getSession: async () => ({ binding: "session-one", expiresAt: Date.now() + 60_000 }),
    listPatients: async () => [{ alias: "mock-alpha", label: "MOCK Alpha" }],
    getView: async () => {
      const view = structuredClone(VIEWS["mock-alpha"]!);
      view.active = { generation_id: GID, retrieved_at: 1793977200, promoted_at: 1793977205 };
      view.provenance!.generation_id = GID;
      view.serving = serving;
      return view;
    },
    ask,
    ...extra,
  };
}

describe("HTTP client owner actions", () => {
  const session = () => reply({ csrf_token: "c".repeat(43) });

  it("asks with the CSRF token, a JSON body of only the question, and the cache acknowledgement only when given", async () => {
    fetchSpy.mockImplementation((path: string) => (path === "/beta/auth/session" ? session() : reply(sampleAnswer())));
    await httpClient.ask!("mock-alpha", GID, "What is the latest creatinine?", false);
    await httpClient.ask!("mock-alpha", GID, "What is the latest creatinine?", true);
    const posts = (fetchSpy.mock.calls as [string, RequestInit][]).filter(([, init]) => init.method === "POST");
    expect(posts.map(([path]) => path)).toEqual([`/beta/epic/patients/mock-alpha/generations/${GID}/runs`, `/beta/epic/patients/mock-alpha/generations/${GID}/runs`]);
    const [first, second] = posts.map(([, init]) => init);
    for (const init of [first!, second!]) {
      expect(init.body).toBe(JSON.stringify({ question: "What is the latest creatinine?" }));
      expect(init.headers).toMatchObject({ "X-CSRF-Token": "c".repeat(43), "Content-Type": "application/json" });
      expect(init).not.toHaveProperty("referrerPolicy");
      expect(init).not.toHaveProperty("mode");
    }
    expect(first!.headers).not.toHaveProperty("X-Beta-Cache-Ack");
    expect(second!.headers).toMatchObject({ "X-Beta-Cache-Ack": "1" });
  });

  it("never sends a malformed alias, generation id or question", async () => {
    await expect(httpClient.ask!("../x", GID, "q", false)).rejects.toBeInstanceOf(UnknownPatientError);
    await expect(httpClient.ask!("mock-alpha", "not-a-generation", "q", false)).rejects.toMatchObject({ code: "generation_unavailable" });
    await expect(httpClient.ask!("mock-alpha", GID, "", false)).rejects.toMatchObject({ code: "invalid_question" });
    await expect(httpClient.ask!("mock-alpha", GID, "x".repeat(1001), false)).rejects.toMatchObject({ code: "invalid_question" });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("revokes and signs out with one CSRF POST each and surfaces refusals as codes", async () => {
    fetchSpy.mockImplementation((path: string) => (path === "/beta/auth/session" ? session() : Promise.resolve(new Response(null, { status: 204 }))));
    await httpClient.revoke!("mock-bravo");
    await httpClient.signOut!();
    const posts = (fetchSpy.mock.calls as [string, RequestInit][]).filter(([, init]) => init.method === "POST").map(([path]) => path);
    expect(posts).toEqual(["/beta/epic/patients/mock-bravo/revoke", "/beta/auth/logout"]);
    fetchSpy.mockImplementation((path: string) => (path === "/beta/auth/session" ? session() : reply({ error: { code: "csrf_rejected" } }, 403)));
    await expect(httpClient.signOut!()).rejects.toMatchObject({ status: 403, code: "csrf_rejected" });
  });
});

describe("Case Review Agent page", () => {
  async function openAgent(client: BetaClient) {
    const user = userEvent.setup();
    renderBeta("#/agent", client, 0);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await screen.findByLabelText(/Question/);
    return user;
  }

  it("shows the verified answer with numbered citations that resolve to the returned evidence", async () => {
    const ask = vi.fn(() => Promise.resolve(sampleAnswer()));
    const user = await openAgent(askingClient(ask));
    await user.type(screen.getByLabelText(/Question/), "What is the latest creatinine?");
    await user.click(screen.getByRole("button", { name: "Ask" }));

    expect(ask).toHaveBeenCalledWith("mock-alpha", GID, "What is the latest creatinine?", false);
    expect(await screen.findByText("Verified 2 of 2 claims")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Claims" })).toHaveTextContent("computed: difference(1, 1.1) = 0.1");
    expect(screen.getByText(/Medication orders were not imported/)).toBeInTheDocument();
    expect(screen.getByText(/Independent review required and pending/)).toBeInTheDocument();
    expect(screen.getByText(/Deterministic mode: no language model was called/)).toBeInTheDocument();
    const evidence = screen.getByRole("list", { name: "Evidence" });
    expect(within(evidence).getAllByRole("listitem")).toHaveLength(3);
    expect(within(evidence).getByText("Retrieved, not cited")).toBeInTheDocument();

    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    await user.click(screen.getAllByRole("button", { name: "Evidence 2" })[0]!);
    expect(scrolled).toHaveBeenCalled();
    expect(document.activeElement).toHaveTextContent("Creatinine 1.1 mg/dL on 2026-10-15.");
  });

  it("marks a citation that matches no returned evidence instead of inventing a source", async () => {
    const answer = sampleAnswer();
    answer.answer!.claims[0]!.citations = ["missing@v9"];
    const user = await openAgent(askingClient(() => Promise.resolve(answer)));
    await user.type(screen.getByLabelText(/Question/), "q");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Unresolved")).toBeInTheDocument();
  });

  it("shows a refusal as a refusal, with no claims, no verification and no evidence", async () => {
    const refused = sampleAnswer({
      outcome: "refused",
      answer: { summary: null, claims: [], uncertainty: [], abstain_reason: null },
      refusal: { category: "clinical_decision", message: "This prototype does not diagnose, prescribe, or recommend treatment." },
      evidence: [],
      verification: null,
    });
    const user = await openAgent(askingClient(() => Promise.resolve(refused)));
    await user.type(screen.getByLabelText(/Question/), "Should I start an antibiotic?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/does not diagnose, prescribe/)).toBeInTheDocument();
    expect(screen.getByText("Refused")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Claims" })).not.toBeInTheDocument();
    expect(screen.queryByText(/^Verified/)).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Evidence" })).not.toBeInTheDocument();
  });

  it("renders answer text as text, never as markup", async () => {
    const answer = sampleAnswer();
    answer.answer!.claims[0]!.text = "<img src=x onerror=alert(1)> <b>bold</b>";
    const user = await openAgent(askingClient(() => Promise.resolve(answer)));
    await user.type(screen.getByLabelText(/Question/), "q");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(document.querySelector("main img")).toBeNull();
    expect(document.querySelector("main b")).toBeNull();
  });

  it("asks for an explicit acknowledgement before using cached data, then asks again with it", async () => {
    const ask = vi
      .fn<NonNullable<BetaClient["ask"]>>()
      .mockRejectedValueOnce(new BetaApiError(409, "cache_acknowledgement_required"))
      .mockResolvedValueOnce(sampleAnswer({ serving: "stale" }));
    const user = await openAgent(askingClient(ask, {}, "stale"));
    expect(screen.getByText("Data is stale")).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Question/), "q");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    const confirm = await screen.findByRole("button", { name: "Ask on stale data" });
    expect(ask).toHaveBeenCalledTimes(1);
    await user.click(confirm);
    expect(ask).toHaveBeenLastCalledWith("mock-alpha", GID, "q", true);
    expect(await screen.findByText("Verified 2 of 2 claims")).toBeInTheDocument();
  });

  it.each([
    [new BetaApiError(403, "patient_scope"), /different patient/],
    [new BetaApiError(410, "generation_expired"), /too old/],
    [new BetaApiError(409, "generation_integrity"), /integrity check/],
    [new BetaApiError(409, "clinical_run_failed"), /could not be answered/],
    [new BetaApiError(401, "owner_required"), /Sign in again/],
  ])("explains a refused question in plain words (%#)", async (error, message) => {
    const user = await openAgent(askingClient(() => Promise.reject(error)));
    await user.type(screen.getByLabelText(/Question/), "q");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByRole("status")).toHaveTextContent(message);
    expect(screen.queryByText("Answer")).not.toBeInTheDocument();
  });

  it("offers no question box without a verified generation, or without a client that can ask", async () => {
    const empty: BetaClient = askingClient(vi.fn());
    const original = empty.getView;
    empty.getView = async (alias) => ({ ...(await original(alias)), active: null, serving: "unavailable", record: null });
    const user = userEvent.setup();
    const { unmount } = renderBeta("#/agent", empty, 0);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    expect(await screen.findByText("No usable generation")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Question/)).not.toBeInTheDocument();
    unmount();

    renderBeta("#/agent", fixtureClient, 0);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    expect(await screen.findByText("Questions are not available")).toBeInTheDocument();
  });
});

describe("Sign out and revoke", () => {
  it("signs out, forgets the selected patient and shows only the sign-in card", async () => {
    const signOut = vi.fn(() => Promise.resolve());
    const client: BetaClient = { ...fixtureClient, signOut };
    const user = userEvent.setup();
    renderBeta("#/patients", client);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await screen.findByText("Clinical record");
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("Signed out")).toBeInTheDocument();
    expect(signOut).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Clinical record")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /MOCK Alpha/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in as owner" }).closest("form")!.getAttribute("action")).toBe("/beta/auth/browser-login");
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("keeps the session on screen and says so when sign-out fails", async () => {
    const client: BetaClient = { ...fixtureClient, signOut: () => Promise.reject(new BetaApiError(403, "csrf_rejected")) };
    const user = userEvent.setup();
    renderBeta("#/patients", client);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText(/Sign-out failed/)).toBeInTheDocument();
    expect(screen.queryByText("Signed out")).not.toBeInTheDocument();
  });

  it("revokes only after an explicit confirmation, then drops the patient from the list", async () => {
    let revoked = false;
    const revoke = vi.fn(() => {
      revoked = true;
      return Promise.resolve();
    });
    const client: BetaClient = {
      ...fixtureClient,
      listPatients: async () => (revoked ? [{ alias: "mock-bravo", label: "MOCK Bravo" }] : await fixtureClient.listPatients()),
      revoke,
    };
    const user = userEvent.setup();
    renderBeta("#/patients", client);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await user.click(await screen.findByRole("button", { name: "Revoke access…" }));
    expect(revoke).not.toHaveBeenCalled(); // one click only arms it
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(revoke).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Revoke access…" }));
    await user.click(screen.getByRole("button", { name: /Confirm: revoke this patient/ }));

    expect(revoke).toHaveBeenCalledWith("mock-alpha");
    expect(await screen.findByText("Select a patient")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /MOCK Alpha/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /MOCK Bravo/ })).toBeInTheDocument();
  });

  it("offers neither control with a client that cannot perform them", async () => {
    const user = userEvent.setup();
    renderBeta("#/patients");
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await screen.findByText("Clinical record");
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke access…" })).not.toBeInTheDocument();
  });
});

describe("Overview (passive MOCK data)", () => {
  it("reads each approved alias separately and shows backend coverage, warnings and provenance without owner actions", async () => {
    const getView = vi.fn(fixtureClient.getView);
    const refresh = vi.fn();
    const ask = vi.fn();
    renderBeta("#/overview", { ...fixtureClient, getView, refresh, ask });
    expect(await screen.findByRole("table", { name: "MOCK Alpha source coverage" })).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "MOCK Bravo source coverage" })).toBeInTheDocument();
    expect(getView.mock.calls.map(([alias]) => alias)).toEqual(["mock-alpha", "mock-bravo"]);
    expect(screen.getAllByText("Serving: Fresh")).toHaveLength(2);
    expect(screen.getAllByText(/Medications are not imported/)).toHaveLength(2);
    expect(screen.getAllByText("Provenance")).toHaveLength(2);
    expect(refresh).not.toHaveBeenCalled();
    expect(ask).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("shows no generation, unknown counts and unavailable coverage honestly", async () => {
    const client = askingClient(vi.fn(), { getView: async () => serverView({ active: null, attempt: null, coverage: null, provenance: null, record: null, serving: "unavailable" }) });
    renderBeta("#/overview", client);
    expect(await screen.findByText("No attempt yet")).toBeInTheDocument();
    expect(screen.getByText("No verified coverage")).toBeInTheDocument();
    expect(screen.getByText("Serving: Unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("Provenance")).not.toBeInTheDocument();
  });

  it.each(["stale", "cached", "rolled_back"] as const)("keeps %s serving state visible", async (serving) => {
    renderBeta("#/overview", askingClient(vi.fn(), {}, serving));
    expect(await screen.findByText(`Serving: ${serving === "rolled_back" ? "Rolled Back" : serving[0]!.toUpperCase() + serving.slice(1)}`)).toBeInTheDocument();
  });

  it("handles expired sessions and an empty approved list without fixture fallback", async () => {
    const { unmount } = renderBeta("#/overview", { ...fixtureClient, listPatients: async () => { throw new BetaApiError(401, "owner_required"); } });
    expect(await screen.findByText("Owner sign-in required")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    unmount();
    renderBeta("#/overview", { ...fixtureClient, listPatients: async () => [] });
    expect(await screen.findByText("No approved patients")).toBeInTheDocument();
  });
});

describe("Passive session fingerprint", () => {
  const session = (overrides = {}) => ({ authenticated: true, mode: "MOCK", csrf_token: "c".repeat(43), server_time_unix_s: 1000, idle_expires_at_unix_s: 1100, absolute_expires_at_unix_s: 2000, ...overrides });
  it("fingerprints the current login without retaining its CSRF value and uses server-relative expiry", async () => {
    fetchSpy.mockImplementation(() => reply(session()));
    const stamp = await httpClient.getSession!();
    expect(stamp.binding).toMatch(/^[a-f0-9]{64}$/);
    expect(JSON.stringify(stamp)).not.toContain("c".repeat(43));
    expect(stamp.expiresAt - Date.now()).toBeGreaterThan(99_000);
    expect(await httpClient.getSession!()).toMatchObject({ binding: stamp.binding });
    fetchSpy.mockImplementation(() => reply(session({ csrf_token: "d".repeat(43) })));
    expect((await httpClient.getSession!()).binding).not.toBe(stamp.binding);
    expect(fetchSpy.mock.calls.every(([path, init]) => path === "/beta/auth/session" && init.method === "GET")).toBe(true);
  });
  it.each([{ authenticated: false }, { mode: "LIVE" }, { csrf_token: "bad" }, { idle_expires_at_unix_s: 999 }, { server_time_unix_s: null }])("rejects an unusable session (%#)", async (change) => {
    fetchSpy.mockImplementation(() => reply(session(change)));
    await expect(httpClient.getSession!()).rejects.toMatchObject({ code: "owner_required" });
  });
});

describe("Evidence Explorer admission and clearing", () => {
  async function returnedAnswer(client: BetaClient) {
    const user = userEvent.setup();
    renderBeta("#/agent", client, 0);
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));
    await user.type(await screen.findByLabelText(/Question/), "q");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Verified 2 of 2 claims");
    return user;
  }
  const go = async (user: ReturnType<typeof userEvent.setup>, name: string) => {
    await user.click(within(screen.getByRole("navigation")).getByRole("button", { name }));
    await screen.findByRole("heading", { level: 1, name });
  };

  it("reuses exactly the returned answer and evidence after navigation without rerunning a question", async () => {
    const ask = vi.fn(async () => sampleAnswer());
    const user = await returnedAnswer(askingClient(ask));
    await go(user, "Evidence Explorer");
    expect(await screen.findByText("Verified 2 of 2 claims")).toBeInTheDocument();
    expect(screen.getAllByText(GID)).toHaveLength(2);
    expect(screen.getByText("run-1")).toBeInTheDocument();
    expect(screen.getByText(/Independent review required and pending/)).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Claims" })).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Creatinine 1.0 mg/dL on 2026-10-12.[1]", "Creatinine changed by 0.1.computed: difference(1, 1.1) = 0.1[1][2]",
    ]);
    expect(within(screen.getByRole("list", { name: "Evidence" })).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByText("Observation/obs-1 v1")).toBeInTheDocument();
    expect(screen.getByText("Retrieved, not cited")).toBeInTheDocument();
    Element.prototype.scrollIntoView = vi.fn();
    await user.click(screen.getAllByRole("button", { name: "Evidence 2" })[0]!);
    expect(document.activeElement).toHaveTextContent("Creatinine 1.1 mg/dL");
    expect(ask).toHaveBeenCalledTimes(1);
  });

  it.each(["session changed", "session failed", "revoked", "wrong alias", "new generation", "wrong provenance", "integrity", "unavailable", "missing record", "LIVE"])("withholds retained evidence when %s", async (boundary) => {
    const client = askingClient(async () => sampleAnswer());
    const user = await returnedAnswer(client);
    const original = client.getView;
    if (boundary === "session changed") client.getSession = async () => ({ binding: "another-login", expiresAt: Date.now() + 60_000 });
    if (boundary === "session failed") client.getSession = async () => { throw new BetaApiError(401, "owner_required"); };
    if (boundary === "revoked") client.listPatients = async () => [];
    client.getView = async (alias) => {
      const view = await original(alias);
      if (boundary === "wrong alias") view.alias = "mock-bravo";
      if (boundary === "new generation") view.active!.generation_id = "b".repeat(32);
      if (boundary === "wrong provenance") view.provenance!.generation_id = "b".repeat(32);
      if (boundary === "integrity") view.evidence_error = "generation_integrity";
      if (boundary === "unavailable") view.serving = "unavailable";
      if (boundary === "missing record") view.record = null;
      if (boundary === "LIVE") Object.assign(view, { mode: "LIVE" });
      return view;
    };
    await go(user, "Evidence Explorer");
    await screen.findByText(boundary.startsWith("session") ? "Session ended" : "Evidence withheld");
    expect(screen.queryByRole("list", { name: "Evidence" })).not.toBeInTheDocument();
    expect(screen.queryByText("run-1")).not.toBeInTheDocument();
    await go(user, "Overview");
    await go(user, "Evidence Explorer");
    expect(await screen.findByText("No retained answer")).toBeInTheDocument();
  });

  it("withholds evidence throughout validation and detects a login change during its reads", async () => {
    const client = askingClient(async () => sampleAnswer());
    const user = await returnedAnswer(client);
    const original = client.getView;
    let resolve!: (view: BetaView) => void;
    client.getView = () => new Promise((done) => { resolve = done; });
    await go(user, "Evidence Explorer");
    await waitFor(() => expect(resolve).toBeTypeOf("function"));
    expect(screen.queryByRole("list", { name: "Evidence" })).not.toBeInTheDocument();
    client.getSession = async () => ({ binding: "another-login", expiresAt: Date.now() + 60_000 });
    await act(async () => resolve(await original("mock-alpha")));
    expect(await screen.findByText("Session ended")).toBeInTheDocument();
  });

  it.each(["session", "generation"])("withdraws already visible evidence after a passive %s check fails", async (boundary) => {
    const client = askingClient(async () => sampleAnswer());
    const user = await returnedAnswer(client);
    await go(user, "Evidence Explorer");
    await screen.findByText("Verified 2 of 2 claims");
    if (boundary === "session") client.getSession = async () => { throw new BetaApiError(401, "owner_required"); };
    else {
      const original = client.getView;
      client.getView = async (alias) => ({ ...(await original(alias)), active: null });
    }
    await screen.findByText(boundary === "session" ? "Session ended" : "Evidence withheld");
    expect(screen.queryByRole("list", { name: "Evidence" })).not.toBeInTheDocument();
  });

  it("clears the last run on a patient change, revocation and sign-out", async () => {
    const client = askingClient(async () => sampleAnswer(), { listPatients: fixtureClient.listPatients, revoke: async () => {}, signOut: async () => {} });
    const user = await returnedAnswer(client);
    await user.click(screen.getByRole("button", { name: /MOCK Bravo/ }));
    await user.click(screen.getByRole("button", { name: /MOCK Alpha/ }));
    await go(user, "Evidence Explorer");
    expect(await screen.findByText("No retained answer")).toBeInTheDocument();
    await go(user, "Case Review Agent");
    await user.type(await screen.findByLabelText(/Question/), "q");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Verified 2 of 2 claims");
    await go(user, "Patients");
    await user.click(await screen.findByRole("button", { name: "Revoke access…" }));
    await user.click(screen.getByRole("button", { name: /Confirm: revoke this patient/ }));
    await go(user, "Evidence Explorer");
    expect(await screen.findByText("No retained answer")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("Signed out")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Evidence" })).not.toBeInTheDocument();
  });
});
