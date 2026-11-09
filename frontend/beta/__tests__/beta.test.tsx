import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BetaApp } from "../BetaApp";
import { fixtureClient, UnknownPatientError } from "../client";
import { BETA_ROUTES, BetaProvider, parseRoute } from "../state";

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchSpy = vi.fn(() => Promise.reject(new Error("network is disabled in Beta MOCK tests")));
  vi.stubGlobal("fetch", fetchSpy);
  window.location.hash = "";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderBeta(hash = "#/patients") {
  window.location.hash = hash;
  return render(
    <BetaProvider>
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

describe("Beta patients page (MOCK fixtures)", () => {
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
    expect(screen.getByText(/Note bodies are not imported/)).toBeInTheDocument();
    expect(screen.getByText("Serving: Fresh")).toBeInTheDocument();
  });

  it("keeps refresh disabled and evidence a placeholder with no citations", async () => {
    const user = userEvent.setup();
    renderBeta();
    await user.click(await screen.findByRole("button", { name: /MOCK Alpha/ }));

    expect(await screen.findByRole("button", { name: /Refresh unavailable/ })).toBeDisabled();
    expect(screen.getByText("Placeholder: no answer has been run in this slice")).toBeInTheDocument();
    expect(screen.getByText("None yet")).toBeInTheDocument();
    expect(screen.getByText(/Pending: not requested/)).toBeInTheDocument();
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
  it("never imports Stable or Streamlit code", () => {
    const sources = import.meta.glob(["../*.ts", "../*.tsx"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    expect(Object.keys(sources).length).toBeGreaterThanOrEqual(8);
    for (const [file, text] of Object.entries(sources)) {
      const specifiers = [...text.matchAll(/(?:from|import)\s*\(?\s*["']([^"']+)["']/g)].map((m) => m[1] ?? "");
      for (const spec of specifiers) {
        const relative = spec.startsWith(".");
        expect(!relative || spec.startsWith("./"), `${file} imports ${spec}`).toBe(true);
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
      await expect(fixtureClient.getRecord(alias)).rejects.toBeInstanceOf(UnknownPatientError);
      await expect(fixtureClient.getStatus(alias)).rejects.toBeInstanceOf(UnknownPatientError);
    }
  });

  it("returns copies so callers cannot mutate the fixtures", async () => {
    const first = await fixtureClient.getRecord("mock-alpha");
    first.labs[0]!.value = 99;
    expect((await fixtureClient.getRecord("mock-alpha")).labs[0]!.value).toBe(1.2);
  });
});
