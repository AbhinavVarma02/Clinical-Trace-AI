// Hosted LIVE client and page. Every value is invented from the MOCK fixtures; fetch is stubbed, nothing leaves the test.
import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { BetaView } from "../contract";
import { VIEWS } from "../fixtures";
import { LiveApiError, checkLiveView, describe, liveApprove, liveRecord, liveRefresh } from "../live";
import { LiveBetaApp } from "../LiveBetaApp";

const GID = "a".repeat(32);

/** Invented values with every marker set consistently to the Epic origin, to exercise the accept path. */
function epicShaped(): BetaView {
  const base = structuredClone(VIEWS["mock-alpha"]!);
  const view: BetaView = {
    ...base,
    mode: "LIVE",
    source: "epic-fhir-sandbox-r4",
    data_origin: "epic_sandbox",
    alias: "invented-alpha",
    label: "Approved LIVE patient",
    serving: "fresh",
    freshness: "fresh",
    publication: "disabled",
    completeness: "unconfirmed",
    evidence_error: null,
    active: { generation_id: GID, retrieved_at: 1793977200, promoted_at: 1793977205 },
    provenance: { ...base.provenance!, generation_id: GID, mode: "LIVE", source: "epic-fhir-sandbox-r4", bundle_sha256: "b".repeat(64), manifest_sha256: "c".repeat(64), synthetic_transport: false } as BetaView["provenance"],
    warnings: [
      { code: "hosted_owner_only", severity: "info", message: "Owner-only hosted Beta (invented test)." },
      { code: "clinical_ai_disabled", severity: "warning", message: "Clinical AI and publication remain disabled." },
    ],
  };
  view.record!.notes.items = view.record!.notes.items.map((item) => ({ ...item, source: { reference: "DocumentReference/n1", version: null, last_updated: null, synthetic: false } }));
  return view;
}

const REVIEW = {
  mode: "LIVE", source: "epic-fhir-sandbox-r4", policy: "live-initial-r4-v1", completeness: "unconfirmed", retrieved_at: 1793977200,
  resource_counts: { Patient: 1 }, unresolved_references: 0, note_bodies: "not_imported", patient_display_name: "Invented Alpha",
  document_metadata_count: 0, operations: { Patient: { execution: "succeeded", import: "imported", matches: 1, imported: 1 } },
  http_outcomes: { "OAuth token": [200], Patient: [200] }, source_timestamps: {},
  mapping: { mapping_version: "x", received: {}, mapped: {}, rejected: {}, field_status: {}, skipped_types: {}, dangling_references: 0 },
};
const CANDIDATE = {
  alias: "invented-alpha", patient_identifier: "invented-live-alpha", synthetic_transport: false, review: REVIEW,
  candidate: { attempt_id: "x".repeat(32), generation_id: GID, manifest_sha256: "c".repeat(64), bundle_sha256: "b".repeat(64) },
};
const SESSION = { owner: true, mode: "LIVE", source: "epic-fhir-sandbox-r4", patient: { alias: "invented-alpha" }, epic_enabled: true, server_time: 1, access_expires: 2 };

function reply(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(body === null ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

beforeEach(() => window.history.replaceState(null, "", "/beta/app/#/overview"));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("accepts a consistently labelled Epic view and refuses fixtures or relabelled data", () => {
  expect(checkLiveView(epicShaped()).alias).toBe("invented-alpha");
  const fixture = epicShaped();
  fixture.data_origin = "synthetic_live_fixture";
  expect(() => checkLiveView(fixture)).toThrow("Live record refused");
  const flagged = epicShaped();
  (flagged.provenance as unknown as Record<string, unknown>).synthetic_transport = true;
  expect(() => checkLiveView(flagged)).toThrow("Live record refused");
  const warned = epicShaped();
  warned.warnings.push({ code: "synthetic_live_fixture", severity: "info", message: "x" });
  expect(() => checkLiveView(warned)).toThrow("Live record refused");
  const note = epicShaped();
  (note.record!.notes.items[0] as unknown as { source: { synthetic: boolean } }).source.synthetic = true;
  expect(() => checkLiveView(note)).toThrow("Live record refused");
  const unhosted = epicShaped();
  unhosted.warnings = unhosted.warnings.filter((warning) => warning.code !== "hosted_owner_only");
  expect(() => checkLiveView(unhosted)).toThrow("Live record refused");
  const published = epicShaped();
  published.publication = "published_snapshot";
  expect(() => checkLiveView(published)).toThrow("Live record refused");
});

it("sends same-origin requests with the client header and no credentials beyond the Access cookie", async () => {
  const fetchMock = vi.fn(async () => reply(200, CANDIDATE));
  vi.stubGlobal("fetch", fetchMock);
  const candidate = await liveRefresh();
  expect(candidate.provenance.manifest_sha256).toBe("c".repeat(64));
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/beta/api/refresh");
  expect(init).toMatchObject({ method: "POST", credentials: "same-origin", mode: "same-origin", redirect: "manual", cache: "no-store" });
  expect((init.headers as Record<string, string>)["X-Beta-Client"]).toBe("live");

  fetchMock.mockImplementationOnce(async () => reply(200, epicShaped()));
  await liveApprove(candidate.provenance);
  const [, approveInit] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
  expect(JSON.parse(approveInit.body as string)).toEqual({ attempt_id: "x".repeat(32), manifest_sha256: "c".repeat(64) });
});

it("refuses synthetic candidates and maps refusals to fixed messages", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => reply(200, { ...CANDIDATE, synthetic_transport: true })));
  await expect(liveRefresh()).rejects.toThrow("Live candidate refused");
  vi.stubGlobal("fetch", vi.fn(async () => reply(429, { error: { code: "quota_exhausted" } }, { "Retry-After": "300" })));
  const error = await liveRefresh().catch((value: unknown) => value);
  expect(error).toBeInstanceOf(LiveApiError);
  expect(describe(error)).toContain("Retry in about 5 min");
  vi.stubGlobal("fetch", vi.fn(async () => reply(404, { error: { code: "generation_unavailable" } })));
  expect(await liveRecord()).toBeNull();
});

it("fetches, reviews and approves before showing the record", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(url);
    if (url.endsWith("/session")) return reply(200, SESSION);
    if (url.endsWith("/record")) return calls.includes("/beta/api/approve") ? reply(200, epicShaped()) : reply(404, { error: { code: "generation_unavailable" } });
    if (url.endsWith("/status")) return reply(200, { attempt: null, candidate: null, generation: null });
    if (url.endsWith("/refresh")) return reply(200, CANDIDATE);
    if (url.endsWith("/approve")) return reply(200, epicShaped());
    return reply(404, { error: { code: "not_found" } });
  }));
  render(<LiveBetaApp />);
  const fetchButton = await screen.findByRole("button", { name: "Fetch from Epic" });
  await screen.findByText(/Approved patient alias invented-alpha/);
  await act(async () => { fireEvent.click(fetchButton); });
  expect(await screen.findByText("Invented Alpha")).toBeInTheDocument();
  expect(calls).toContain("/beta/api/refresh");
  expect(calls).not.toContain("/beta/api/approve");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Approve and display this candidate" })); });
  expect(calls).toContain("/beta/api/approve");
  expect(await screen.findAllByText("Epic sandbox synthetic patient")).not.toHaveLength(0);
});
