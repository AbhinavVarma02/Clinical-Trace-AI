// UI lifetime boundary only; accepted resource rendering suites are not repeated.
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GenuineBetaApp } from "../GenuineBetaApp";
import { genuineOwnerActions, genuineOwnerOperation, genuineOwnerRead, pendingOwnerEnrollment } from "../genuine";
import type { BetaView } from "../contract";

vi.mock("../genuine", () => ({ genuineOwnerRead: vi.fn(), genuineOwnerActions: vi.fn(), genuineOwnerOperation: vi.fn(), pendingOwnerEnrollment: vi.fn(), completeOwnerEnrollment: vi.fn() }));
vi.mock("../webauthn", () => ({ OWNER_ORIGIN: window.location.origin }));
vi.mock("../PatientRecord", () => ({ RecordTabs: () => <div>INVENTED CONTRACT sentinel</div>, WarningList: () => null, RefreshStatus: () => null, Provenance: () => null }));
const double = { record: {}, active: { generation_id: "a".repeat(32) }, warnings: [] } as unknown as BetaView;
beforeEach(() => {
  window.history.replaceState(null, "", "/");
  vi.useFakeTimers();
  vi.stubGlobal("isSecureContext", true);
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  vi.mocked(genuineOwnerRead).mockResolvedValue({ result: double, expiresAt: Date.now() + 30000 });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.clearAllMocks(); });
async function read() { await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Read approved patient" })); }); }

it("starts no background authentication and clears after the bounded display interval", async () => {
  render(<GenuineBetaApp />);
  expect(genuineOwnerRead).not.toHaveBeenCalled();
  await read();
  expect(screen.getByText("INVENTED CONTRACT sentinel")).toBeInTheDocument();
  await act(async () => { vi.advanceTimersByTime(30001); });
  expect(screen.queryByText("INVENTED CONTRACT sentinel")).not.toBeInTheDocument();
  expect(genuineOwnerRead).toHaveBeenCalledTimes(1);
});

it.each(["clear", "blur", "failure"])("withholds retained content on %s", async (change) => {
  render(<GenuineBetaApp />); await read();
  if (change === "clear") fireEvent.click(screen.getByRole("button", { name: "Clear view" }));
  if (change === "blur") fireEvent.blur(window);
  if (change === "failure") { vi.mocked(genuineOwnerRead).mockRejectedValueOnce(new Error("refused")); await read(); }
  expect(screen.queryByText("INVENTED CONTRACT sentinel")).not.toBeInTheDocument();
});

it.each(["clear", "unfocused"])("fences a late result after %s", async (change) => {
  let finish!: (value: { result: BetaView; expiresAt: number }) => void;
  vi.mocked(genuineOwnerRead).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
  render(<GenuineBetaApp />); fireEvent.click(screen.getByRole("button", { name: "Read approved patient" }));
  if (change === "clear") fireEvent.click(screen.getByRole("button", { name: "Clear view" }));
  else vi.mocked(document.hasFocus).mockReturnValue(false);
  await act(async () => { finish({ result: double, expiresAt: Date.now() + 30000 }); });
  expect(screen.queryByText("INVENTED CONTRACT sentinel")).not.toBeInTheDocument();
});

it("refuses a read finishing after a route change before the hashchange event is handled", async () => {
  let finish!: (value: { result: BetaView; expiresAt: number }) => void;
  vi.mocked(genuineOwnerRead).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
  render(<GenuineBetaApp />);
  fireEvent.click(screen.getByRole("button", { name: "Read approved patient" }));
  window.history.replaceState(null, "", "/#/evidence");
  await act(async () => { finish({ result: double, expiresAt: Date.now() + 30000 }); });
  expect(screen.queryByText("INVENTED CONTRACT sentinel")).not.toBeInTheDocument();
});

it("keeps the enrollment comparison visible during a console focus switch without disclosing the binding", async () => {
  const fingerprint = "a".repeat(64);
  vi.mocked(pendingOwnerEnrollment).mockResolvedValue({ pending_id: "invented-id", binding: "invented-binding", fingerprint });
  render(<GenuineBetaApp />);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Request owner enrollment" })); });
  expect(screen.getByText(fingerprint)).toBeInTheDocument();
  expect(screen.queryByText("invented-binding")).not.toBeInTheDocument();
  fireEvent.blur(window);
  fireEvent.focus(window);
  expect(screen.getByText(fingerprint)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Complete approved enrollment" })).toBeEnabled();
});

it("keeps the eight-page frame and discards protected content on navigation and pagehide", async () => {
  render(<GenuineBetaApp />);
  const sidebar = screen.getByRole("navigation");
  expect(within(sidebar).getAllByRole("button").map((button) => button.textContent)).toEqual([
    "Overview", "Patients", "Case Review Agent", "Risk & Explainability", "Evidence Explorer", "Evaluation", "Operations", "Governance",
  ]);
  await read();
  expect(screen.getByText("INVENTED CONTRACT sentinel")).toBeInTheDocument();
  await act(async () => {
    fireEvent.click(within(sidebar).getByRole("button", { name: "Case Review Agent" }));
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  expect(screen.getByRole("heading", { name: "Case Review Agent" })).toBeInTheDocument();
  expect(screen.getByText("Workflow disabled in Epic Beta")).toBeInTheDocument();
  expect(screen.queryByText("INVENTED CONTRACT sentinel")).not.toBeInTheDocument();
  expect(genuineOwnerRead).toHaveBeenCalledTimes(1);
  await act(async () => {
    fireEvent.click(within(sidebar).getByRole("button", { name: "Patients" }));
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await read();
  expect(screen.getByText("INVENTED CONTRACT sentinel")).toBeInTheDocument();
  fireEvent(window, new Event("pagehide"));
  expect(screen.queryByText("INVENTED CONTRACT sentinel")).not.toBeInTheDocument();
});

it("gates promotion on a freshly reviewed candidate and discards review on expiry", async () => {
  window.history.replaceState(null, "", "/#/operations");
  vi.mocked(genuineOwnerActions).mockResolvedValue(["review", "promote"]);
  const provenance = { attempt_id: "a".repeat(32), generation_id: "b".repeat(32), manifest_sha256: "c".repeat(64), bundle_sha256: "d".repeat(64) };
  vi.mocked(genuineOwnerOperation).mockImplementation(async (operation) => ({
    result: { operation, alias: "approved", patient_identifier: "invented-contract", provenance,
      review: { source: "epic-fhir-sandbox-r4", policy: "invented", completeness: "unconfirmed", retrieved_at: 100, resource_counts: { Patient: 1 }, unresolved_references: 0, note_bodies: "not_imported", operations: {},
        patient_display_name: "Invented contract patient", document_metadata_count: 0, http_outcomes: { Patient: [200] },
        mapping: { mapping_version: "invented", received: { Patient: 1 }, mapped: { Patient: 1 }, rejected: {}, field_status: {}, skipped_types: {}, dangling_references: 0 },
        source_timestamps: { Patient: { count: 1, oldest: "2026-11-01T12:00:00Z", newest: "2026-11-01T12:00:00Z" } } } },
    expiresAt: Date.now() + 30000,
  }));
  render(<GenuineBetaApp />);
  expect(genuineOwnerActions).not.toHaveBeenCalled();
  expect(genuineOwnerOperation).not.toHaveBeenCalled();
  const click = async (name: string) => { await act(async () => { fireEvent.click(screen.getByRole("button", { name })); }); };
  await click("Check approved operator actions");
  const promote = screen.getByRole("button", { name: "Promote reviewed candidate" });
  expect(promote).toBeDisabled();
  await click("Review staged candidate");
  expect(promote).toBeEnabled();
  expect(screen.getByText(provenance.manifest_sha256)).toBeInTheDocument();
  expect(screen.getByText("Invented contract patient")).toBeInTheDocument();
  expect(screen.getByText("HTTP outcomes")).toBeInTheDocument();
  expect(screen.getByText("1 timestamps · 2026-11-01T12:00:00Z to 2026-11-01T12:00:00Z")).toBeInTheDocument();
  await act(async () => { vi.advanceTimersByTime(30001); });
  expect(promote).toBeDisabled();
  expect(screen.queryByText(provenance.manifest_sha256)).not.toBeInTheDocument();
  await click("Review staged candidate");
  await click("Promote reviewed candidate");
  expect(genuineOwnerOperation).toHaveBeenLastCalledWith("promote", provenance, expect.any(AbortSignal));
  expect(screen.getByRole("heading", { name: "Candidate promoted" })).toBeInTheDocument();
});
