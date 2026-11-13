// This integration selection requires JSON exported by test_beta_synthetic_ui.py. No hand-authored record
// fallback: fetch replays actual ASGI responses into the production httpClient and Beta components.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
// @ts-expect-error Node built-ins are used only by offline tests, outside the browser type surface.
import { readFileSync } from "node:fs";
// @ts-expect-error Node's crypto is test-only.
import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BetaApp } from "../BetaApp";
import { BetaApiError, httpClient } from "../client";
import type { Answer, BetaView, PatientChoice } from "../contract";
import { BetaProvider, useBeta } from "../state";

interface Captured { status: number; body: unknown }
interface Receipt {
  fixture: string;
  patients: { patients: PatientChoice[] };
  view: BetaView;
  mock_view: BetaView;
  session: Record<string, number | string | boolean>;
  failures: Record<string, Captured>;
}
const receiptPath = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env.CT_SYNTHETIC_UI_RECEIPT;
const captured: Receipt | null = receiptPath ? JSON.parse(readFileSync(receiptPath, "utf8")) as Receipt : null;
const offline = receiptPath ? describe : describe.skip;

function RetentionProbe({ view }: { view: BetaView }) {
  const { alias, latestRun, clearRun, retainRun } = useBeta();
  return <><output aria-label="Retained run">{latestRun ? "Retained" : "Cleared"}</output><button onClick={() => {
    if (alias) retainRun({ alias, generationId: view.active!.generation_id, sessionBinding: "test-only", answer: {} as Answer }, clearRun());
  }}>Capture retention sentinel</button></>;
}

offline("invented FHIR -> verified API -> Beta UI (no browser/listener)", () => {
  let fixture: Receipt;
  let fetchSpy: ReturnType<typeof vi.fn<(path: string, options?: RequestInit) => Promise<Response>>>;
  let failure: Captured | null;
  let sessionFailure: Captured | null;
  let session: Receipt["session"];
  let delayedView: Promise<Response> | null;

  const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
  const alias = () => fixture.view.alias;
  const patientName = () => fixture.view.record!.patient.display_name!;
  const pick = async () => userEvent.setup().click(await screen.findByRole("button", { name: new RegExp(fixture.view.label) }));
  const mount = (hash = "#/patients", pollMs = 10000) => {
    window.location.hash = hash;
    return render(<BetaProvider client={httpClient} pollMs={pollMs}><BetaApp /><RetentionProbe view={fixture.view} /></BetaProvider>);
  };

  beforeEach(() => {
    fixture = structuredClone(captured!);
    expect(fixture.fixture).toContain("INVENTED");
    failure = sessionFailure = null;
    delayedView = null;
    session = fixture.session;
    fetchSpy = vi.fn(async (path: string) => {
      if (path === "/beta/auth/session") return sessionFailure ? response(sessionFailure.body, sessionFailure.status) : response(session);
      if (path === "/beta/epic/patients") return response(fixture.patients);
      if (path === `/beta/epic/patients/${alias()}/record`) {
        if (delayedView) return delayedView;
        return failure ? response(failure.body, failure.status) : response(fixture.view);
      }
      if (path === "/beta/epic/patients/mock-alpha/record") return response(fixture.mock_view);
      throw new Error("unexpected request in offline API replay");
    });
    vi.stubGlobal("fetch", fetchSpy);
    vi.stubGlobal("crypto", webcrypto);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders API patient identity, every supported section, provenance, timestamps and completeness", async () => {
    const user = userEvent.setup();
    mount();
    await pick();
    expect(await screen.findByText(new RegExp(patientName()))).toBeInTheDocument();
    expect(screen.getByText("Completeness: Unconfirmed")).toBeInTheDocument();
    expect(screen.getByText("Serving: Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Evidence freshness: Fresh")).toBeInTheDocument();
    for (const warning of fixture.view.warnings) expect(screen.getByText(warning.message)).toBeInTheDocument();
    const record = fixture.view.record!;
    for (const condition of record.conditions.items) expect(screen.getAllByText(new RegExp(condition.display!)).length).toBeGreaterThan(0);
    const panel = () => screen.getByRole("tabpanel");
    await user.click(screen.getByRole("tab", { name: /Encounters.*1/ }));
    expect(within(panel()).getByText("Finished")).toBeInTheDocument();
    expect(panel().textContent).toContain(record.encounters.items[0]!.source.reference);
    await user.click(screen.getByRole("tab", { name: /Labs.*1/ }));
    expect(within(panel()).getByText("1.2 mg/dL")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Vital signs.*1/ }));
    expect(within(panel()).getByText("70 /min")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Reports.*1/ }));
    expect(panel().textContent).toContain(record.reports.items[0]!.conclusion);
    expect(within(panel()).getByText("2 linked results")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Note metadata.*1/ }));
    expect(panel().textContent).toContain(record.notes.items[0]!.source!.reference);
    expect(panel().textContent).toContain(record.notes.items[0]!.source!.version);
    expect(panel().textContent).toContain("2020");
    expect(within(panel()).getAllByText(record.notes.items[0]!.type!).length).toBeGreaterThan(0);
    expect(within(panel()).getByText("Current")).toBeInTheDocument();
    expect(screen.getByText(fixture.view.provenance!.generation_id)).toBeInTheDocument();
    expect(screen.getByText(fixture.view.provenance!.bundle_sha256!)).toBeInTheDocument();
    expect(screen.getByText(fixture.view.provenance!.manifest_sha256!)).toBeInTheDocument();
    expect(screen.getByText(`${fixture.view.source} / LIVE`)).toBeInTheDocument();
    const operations = screen.getByRole("table", { name: "Per-operation counts" });
    expect(within(operations).getByText("59100")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh unavailable" })).toBeDisabled();
    expect(screen.getByText("No claims or citations have been created.", { exact: false })).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("invented forbidden clinical note body");
    expect(document.body.textContent).not.toContain("Binary/secret");
    expect(screen.queryByRole("heading", { name: "Answer" })).not.toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([path]) => path === `/beta/epic/patients/${alias()}/record`)).toBe(true);
    for (const [, options] of fetchSpy.mock.calls) expect(options).toMatchObject({ method: "GET", mode: "same-origin", credentials: "same-origin", cache: "no-store", redirect: "error" });
  });

  it("shows the API origin/coverage on Overview and withholds synthetic clinical AI", async () => {
    mount("#/overview");
    expect(await screen.findByText("Invented LIVE-shaped fixture; no Epic request")).toBeInTheDocument();
    expect(screen.getByText("Completeness: Unconfirmed")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Case Review Agent" }));
    await pick();
    expect(await screen.findByText("This synthetic LIVE-shaped record is a read-only preview. Clinical AI is disabled.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it.each(["revoked", "expired", "erased", "hash", "mixed_patient"])("clears displayed evidence on the actual API %s refusal", async (change) => {
    mount();
    await pick();
    await screen.findByText(new RegExp(patientName()));
    failure = fixture.failures[change]!;
    act(() => window.dispatchEvent(new Event("focus")));
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
    expect(await screen.findByText("Patient unavailable")).toBeInTheDocument();
    expect(screen.queryByText(fixture.view.provenance!.bundle_sha256!)).not.toBeInTheDocument();
  });

  it("clears evidence on session revocation and rejects a replacement session binding", async () => {
    mount();
    await pick();
    await screen.findByText(new RegExp(patientName()));
    session = { ...session, csrf_token: "B".repeat(43) };
    act(() => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText("Session ended")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
  });

  it("clears on the actual owner-expiry API response", async () => {
    mount();
    await pick();
    await screen.findByText(new RegExp(patientName()));
    sessionFailure = fixture.failures.session_expired!;
    act(() => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText("Session ended")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
  });

  it("clears immediately when the owner revocation action reports expired access", async () => {
    mount();
    await pick();
    await screen.findByText(new RegExp(patientName()));
    const get = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (path: string) => path.endsWith("/revoke")
      ? response(fixture.failures.session_expired!.body, fixture.failures.session_expired!.status) : get(path));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Revoke access…" }));
    await user.click(screen.getByRole("button", { name: "Confirm: revoke this patient" }));
    expect(await screen.findByText("Session ended")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
  });

  it("clears at local expiry even while a later record response is stalled, and ignores its late completion", async () => {
    const serverTime = Number(session.server_time_unix_s);
    session = { ...session, idle_expires_at_unix_s: serverTime + 0.3, absolute_expires_at_unix_s: serverTime + 0.3 };
    mount();
    await pick();
    await screen.findByText(new RegExp(patientName()));
    let release!: (value: Response) => void;
    delayedView = new Promise((resolve) => { release = resolve; });
    act(() => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText("Session ended")).toBeInTheDocument();
    await act(async () => { release(response(fixture.view)); });
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
  });

  it("clears a displayed record at the idle deadline without a foreground event or another response", async () => {
    const serverTime = Number(session.server_time_unix_s);
    session = { ...session, idle_expires_at_unix_s: serverTime + 0.3, absolute_expires_at_unix_s: serverTime + 0.3 };
    mount();
    await pick();
    await screen.findByText(new RegExp(patientName()));
    expect(await screen.findByText("Session ended")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
    expect(screen.queryByText(fixture.view.provenance!.bundle_sha256!)).not.toBeInTheDocument();
  });

  it("clears displayed evidence on a passive revocation refusal without a foreground event", async () => {
    mount("#/patients", 20);
    await pick();
    await screen.findByText(new RegExp(patientName()));
    await userEvent.setup().click(screen.getByRole("button", { name: "Capture retention sentinel" }));
    failure = fixture.failures.revoked!;
    expect(await screen.findByText("Patient unavailable")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
    expect(screen.getByLabelText("Retained run")).toHaveTextContent("Cleared");
  });

  it("never restores a late response beneath a different selected patient", async () => {
    let release!: (value: Response) => void;
    delayedView = new Promise((resolve) => { release = resolve; });
    mount();
    await pick();
    await waitFor(() => expect(fetchSpy.mock.calls.some(([path]) => path === `/beta/epic/patients/${alias()}/record`)).toBe(true));
    await userEvent.setup().click(screen.getByRole("button", { name: /MOCK Alpha/ }));
    await screen.findByText("No verified record yet");
    await act(async () => { release(response(fixture.view)); });
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
    expect(screen.queryByText(fixture.view.provenance!.bundle_sha256!)).not.toBeInTheDocument();
  });

  it("replaces a changed generation and clears prior retained evidence", async () => {
    mount();
    await pick();
    const original = fixture.view.provenance!.generation_id;
    await screen.findByText(original);
    await userEvent.setup().click(screen.getByRole("button", { name: "Capture retention sentinel" }));
    expect(screen.getByLabelText("Retained run")).toHaveTextContent("Retained");
    // A changed backend response is an explicit negative/control mutation, never a display fixture.
    const replacement = "f".repeat(32);
    fixture.view.active!.generation_id = replacement;
    fixture.view.provenance!.generation_id = replacement;
    act(() => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText(replacement)).toBeInTheDocument();
    expect(screen.queryByText(original)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Retained run")).toHaveTextContent("Cleared");
  });

  it("withholds a record when the session changes between record fetch and final session validation", async () => {
    const get = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (path: string) => {
      const result = await get(path);
      if (path === `/beta/epic/patients/${alias()}/record`) session = { ...session, csrf_token: "C".repeat(43) };
      return result;
    });
    mount();
    await pick();
    expect(await screen.findByText("Session ended")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
  });

  it("clears a retained run on passive generation change without a foreground invalidation", async () => {
    mount("#/patients", 20);
    await pick();
    await screen.findByText(fixture.view.provenance!.generation_id);
    await userEvent.setup().click(screen.getByRole("button", { name: "Capture retention sentinel" }));
    expect(screen.getByLabelText("Retained run")).toHaveTextContent("Retained");
    fixture.view.active!.generation_id = "d".repeat(32);
    fixture.view.provenance!.generation_id = "d".repeat(32);
    expect(await screen.findByText("d".repeat(32))).toBeInTheDocument();
    expect(screen.getByLabelText("Retained run")).toHaveTextContent("Cleared");
  });

  it.each(["alias", "generation", "tier", "source", "real_origin", "real_marker", "serving", "mock_tier", "mock_source"])("rejects a mismatched %s response before rendering", async (change) => {
    if (change === "alias") fixture.view.alias = "approved-bravo";
    else if (change === "generation") fixture.view.provenance!.generation_id = "e".repeat(32);
    else if (change === "tier") fixture.view.provenance!.mode = "MOCK";
    else if (change === "source") fixture.view.provenance!.source = "different-source";
    else if (change === "real_origin") (fixture.view as unknown as Record<string, unknown>).data_origin = "epic_sandbox";
    else if (change === "real_marker") fixture.view.provenance!.synthetic_transport = false;
    else if (change === "serving") fixture.view.serving = "fresh";
    else {
      fixture.view.mode = "MOCK";
      fixture.view.data_origin = "synthetic_mock_transport";
      if (change === "mock_source") { fixture.view.provenance!.mode = "MOCK"; fixture.view.provenance!.source = "different-source"; }
    }
    // Keep URL bound to the original requested selection, including the wrong-alias mutation.
    fetchSpy.mockImplementation(async (path: string) => path === "/beta/auth/session" ? response(session) : path === "/beta/epic/patients" ? response(fixture.patients) : response(fixture.view));
    mount();
    await pick();
    expect(await screen.findByText("Patient unavailable")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
  });

  it("charges delayed session responses against the remaining access window", async () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(1000);
    const serverTime = Number(session.server_time_unix_s);
    fetchSpy.mockImplementation(async () => {
      clock.mockReturnValue(3000);
      return response({ ...session, idle_expires_at_unix_s: serverTime + 1, absolute_expires_at_unix_s: serverTime + 1 });
    });
    const result = await httpClient.getSession!();
    expect(result.expiresAt).toBe(2000);
    expect(result.expiresAt).toBeLessThan(Date.now());
  });

  it("handles transport failure without retaining clinical content", async () => {
    mount();
    await pick();
    await screen.findByText(new RegExp(patientName()));
    fetchSpy.mockRejectedValue(new Error("invented offline transport failure"));
    act(() => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText("Patient unavailable")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(patientName()))).not.toBeInTheDocument();
    expect(screen.queryByText("invented offline transport failure")).not.toBeInTheDocument();
  });

  it("continues to surface content-free refusal codes through the HTTP client", async () => {
    failure = fixture.failures.revoked!;
    await expect(httpClient.getView(alias())).rejects.toEqual(new BetaApiError(404, "patient_unavailable"));
  });
});
