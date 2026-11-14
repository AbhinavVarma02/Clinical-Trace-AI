// Hosted read-only copy: strict document checks and a page that shows nothing unless they pass.
// Every value is invented from the MOCK fixtures and labelled as a synthetic LIVE-shaped fixture.
import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { BetaView } from "../contract";
import { VIEWS } from "../fixtures";
import { PUBLICATION_FORMAT, checkPublication, type Publication } from "../published";
import { PublishedBetaApp } from "../PublishedBetaApp";

function invented(): Publication {
  const base = structuredClone(VIEWS["mock-alpha"]!);
  const gid = "a".repeat(32);
  const view: BetaView = {
    ...base,
    mode: "LIVE",
    source: "epic-fhir-sandbox-r4",
    data_origin: "synthetic_live_fixture",
    alias: "invented-alpha",
    label: "Synthetic LIVE-shaped fixture",
    serving: "stale",
    freshness: "stale",
    publication: "published_snapshot",
    completeness: "unconfirmed",
    active: { generation_id: gid, retrieved_at: 1793977200, promoted_at: 1793977205 },
    provenance: { ...base.provenance!, generation_id: gid, mode: "LIVE", source: "epic-fhir-sandbox-r4", bundle_sha256: "b".repeat(64), manifest_sha256: "c".repeat(64), synthetic_transport: true } as BetaView["provenance"],
    warnings: [
      { code: "published_snapshot", severity: "warning", message: "Published read-only copy (invented test)." },
      { code: "synthetic_live_fixture", severity: "info", message: "Test-only synthetic LIVE-shaped evidence. This is not an Epic-acquired patient record." },
    ],
  };
  view.record!.notes.items = view.record!.notes.items.map((item) => ({ ...item, source: { reference: "DocumentReference/n1", version: null, last_updated: null, synthetic: true } }));
  return { format: PUBLICATION_FORMAT, exported_at: 1793977300, withheld: ["attempt_id"], view };
}

/** The same invented values with every marker consistently set to the Epic origin, to exercise the accept path. */
function epicLabelled(): Publication {
  const publication = invented();
  const view = publication.view;
  view.data_origin = "epic_sandbox";
  view.label = "Approved LIVE patient";
  (view.provenance as unknown as Record<string, unknown>).synthetic_transport = false;
  view.warnings = view.warnings.filter((warning) => warning.code !== "synthetic_live_fixture");
  for (const item of view.record!.notes.items) (item as unknown as { source: { synthetic: boolean } }).source.synthetic = false;
  return publication;
}

const respond = (body: unknown, ok = true) => vi.fn(async () => ({ ok, json: async () => body }) as Response);
beforeEach(() => { window.history.replaceState(null, "", "/#/patients"); vi.stubEnv("VITE_BETA_ALLOW_FIXTURE", "true"); });
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.restoreAllMocks(); });

it("accepts a consistently labelled copy with the attempt identity withheld", () => {
  expect(checkPublication(epicLabelled(), false).view.data_origin).toBe("epic_sandbox");
  expect(checkPublication(invented(), true).view.alias).toBe("invented-alpha");
});

it("accepts an invented fixture only in a preview build that allows it", () => {
  expect(() => checkPublication(invented(), false)).toThrow("Published record refused");
  vi.stubEnv("VITE_BETA_ALLOW_FIXTURE", "");
  expect(() => checkPublication(invented())).toThrow("Published record refused");
});

it.each<[string, (p: Publication) => void]>([
  ["a fixture relabelled by one flag", (p) => { p.view.data_origin = "epic_sandbox"; (p.view.provenance as unknown as Record<string, unknown>).synthetic_transport = false; }],
  ["an Epic label on a fixture", (p) => { p.view.label = "Approved LIVE patient"; }],
  ["a fixture without its synthetic notice", (p) => { p.view.warnings = p.view.warnings.filter((w) => w.code !== "synthetic_live_fixture"); }],
  ["a synthetic note source on Epic data", (p) => { Object.assign(p, epicLabelled()); (p.view.record!.notes.items[0] as unknown as { source: { synthetic: boolean } }).source.synthetic = true; }],
])("refuses %s", (_, change) => {
  const publication = invented();
  change(publication);
  expect(() => checkPublication(publication, true)).toThrow("Published record refused");
});

it.each<[string, (p: Publication) => void]>([
  ["another format", (p) => { (p as { format: string }).format = "x"; }],
  ["no withheld attempt", (p) => { p.withheld = []; }],
  ["an attempt identity", (p) => { (p.view.provenance as unknown as Record<string, unknown>).attempt_id = "a".repeat(32); }],
  ["a relabelled fixture", (p) => { p.view.data_origin = "epic_sandbox"; }],
  ["a MOCK view", (p) => { p.view.mode = "MOCK"; }],
  ["live serving", (p) => { p.view.serving = "fresh"; }],
  ["publication disabled", (p) => { p.view.publication = "disabled"; }],
  ["confirmed completeness", (p) => { (p.view as { completeness: string }).completeness = "complete"; }],
  ["an integrity error", (p) => { p.view.evidence_error = "generation_integrity"; }],
  ["a mismatched generation", (p) => { p.view.provenance!.generation_id = "f".repeat(32); }],
  ["a malformed hash", (p) => { p.view.provenance!.bundle_sha256 = "short"; }],
  ["no snapshot notice", (p) => { p.view.warnings = []; }],
  ["a missing section", (p) => { delete (p.view.record as unknown as Record<string, unknown>).labs; }],
])("refuses %s", (_, change) => {
  const publication = invented();
  change(publication);
  expect(() => checkPublication(publication, true)).toThrow("Published record refused");
});

it("loads the copy once, same-origin with no credentials, and renders the existing record views", async () => {
  const fetchMock = respond(invented());
  vi.stubGlobal("fetch", fetchMock);
  await act(async () => { render(<PublishedBetaApp />); });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toMatch(/published\/record\.json$/);
  expect(init).toMatchObject({ method: "GET", credentials: "omit", mode: "same-origin", redirect: "error" });
  expect(screen.getByText(/MOCK Alpha \(synthetic\)/)).toBeInTheDocument();
  expect(screen.getByText("Published read-only copy (invented test).")).toBeInTheDocument();
  expect(screen.getAllByText("Synthetic LIVE-shaped fixture").length).toBeGreaterThan(0);
  expect(screen.getByText(/Epic Beta demonstration\./)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Windows Hello|Read approved/ })).not.toBeInTheDocument();
});

it.each([["a refused document", () => respond({ ...invented(), withheld: [] })], ["a failed request", () => respond({}, false)]])(
  "shows nothing from %s",
  async (_, make) => {
    vi.stubGlobal("fetch", make());
    await act(async () => { render(<PublishedBetaApp />); });
    expect(screen.getByText("Published record unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/MOCK Alpha \(synthetic\)/)).not.toBeInTheDocument();
  },
);

it.each([["operations", "No operator actions in the hosted demonstration"], ["agent", "Workflow disabled in Epic Beta"], ["governance", "Workflow disabled in Epic Beta"]])(
  "keeps the %s page free of owner functions and clinical AI",
  async (route, text) => {
    window.history.replaceState(null, "", `/#/${route}`);
    vi.stubGlobal("fetch", respond(invented()));
    await act(async () => { render(<PublishedBetaApp />); });
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByText(/MOCK Alpha \(synthetic\)/)).not.toBeInTheDocument();
  },
);
