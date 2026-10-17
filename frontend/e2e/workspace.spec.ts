import { expect, test, type Page } from "@playwright/test";
import { DEMOS } from "../workspace/demos";

async function nav(page: Page, name: string) {
  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("navigation").getByRole("button", { name, exact: true }).click();
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/");
  await expect(page.getByRole("button", { name: "Start demo", exact: true })).toBeVisible();
});

for (const demo of DEMOS) {
  test(`guided scenario: ${demo.title}`, async ({ page }) => {
    const response = page.waitForResponse(r => r.url().endsWith("/api/v2/agent/runs") && r.request().method() === "POST");
    await page.getByRole("button", { name: new RegExp(demo.title) }).click();
    const http = await response;
    expect(http.ok()).toBeTruthy();
    const run = await http.json();
    expect(run.patient_id).toBe(demo.patientId);
    expect(run.question).toBe(demo.question);
    expect(run.outcome).toBe("answered");
    expect(run.tool_calls.length).toBeGreaterThan(0);
    expect(run.trace.some((s: { node: string }) => s.node === "verify")).toBeTruthy();
    const answer = page.getByRole("article", { name: `Answer to: ${demo.question}` });
    await expect(answer.getByText("Deterministic · no LLM", { exact: true })).toBeVisible();
    await expect(answer.getByRole("list", { name: "Verified claims" })).toBeVisible();
    // Resolve every cited record through the real API, then inspect a source in the UI.
    for (const claim of run.answer.claims) {
      for (const id of claim.citations) {
        const evidence = run.evidence.find((e: { evidence_id: string }) => e.evidence_id === id);
        expect(evidence).toBeTruthy();
        expect(evidence.patient_id).toBe(demo.patientId);
        const source = await page.request.get(`/fhir/r4/${evidence.source_ref}/_history/${evidence.source_version}`, {
          headers: { "X-Demo-Persona": "clinician.demo" },
        });
        expect(source.ok()).toBeTruthy();
      }
    }
    await answer.getByRole("button", { name: /^Open evidence / }).first().click();
    const drawer = page.getByRole("dialog");
    await expect(drawer.getByText("Evidence text given to the verifier")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
  });
}

test("fresh question, safety refusal, unavailable risk", async ({ page }) => {
  await nav(page, "Case Review Agent");
  const question = "Why was metformin stopped?";
  await page.getByRole("textbox", { name: "Question", exact: true }).fill(question);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByRole("article", { name: `Answer to: ${question}` })).toContainText("metformin");
  await page.getByRole("button", { name: "Should we increase the insulin dose?", exact: true }).click();
  await expect(page.getByRole("article", { name: "Answer to: Should we increase the insulin dose?" })).toContainText("Refused");
  await nav(page, "Risk & Explainability");
  await expect(page.getByText("Model not applicable to this record", { exact: true })).toBeVisible();
});

test("persona scoping and cross-session access fail closed", async ({ page }) => {
  const headers = { "X-Demo-Persona": "clinician.demo" };
  const response = await page.request.post("/api/v2/agent/runs", {
    headers, data: { patient_id: "synthetic_p001", question: "Why was metformin stopped?" },
  });
  expect(response.ok()).toBeTruthy();
  const run = await response.json();
  await page.getByRole("combobox", { name: "Demo persona" }).selectOption("clinician.restricted");
  await nav(page, "Patients");
  await expect(page.getByRole("button", { name: /synthetic_p002/ })).toHaveCount(0);
  const denied = await page.request.get("/api/v2/patients/synthetic_p002", {
    headers: { "X-Demo-Persona": "clinician.restricted" },
  });
  expect(denied.status()).toBe(403);
  const session = await page.request.get(`/api/v2/agent/sessions/${run.session_id}`, {
    headers: { "X-Demo-Persona": "clinician.restricted" },
  });
  expect(session.status()).toBe(403);
});

test("submit, inspect, and approve an exact review artifact", async ({ page }) => {
  await page.getByRole("button", { name: /Contradictory notes/ }).click();
  await page.getByRole("button", { name: "Submit for human review", exact: true }).click();
  await expect(page.getByText(/Submitted as review/)).toBeVisible();
  await page.getByRole("combobox", { name: "Demo persona" }).selectOption("reviewer.demo");
  await nav(page, "Governance");
  await expect(page.getByText("Is the smoking history consistent across notes?", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByText("Approval is valid for the exact artifact version shown.")).toBeVisible();
});

test("responsive navigation, keyboard focus, persisted themes, and real errors", async ({ page }) => {
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.getByRole("button", { name: "Switch to dark theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await nav(page, "Evaluation");
  await expect(page.getByRole("heading", { name: /Evaluation/ }).first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  // Administrator has ops access but no clinical-data access: exercise the real error state.
  await page.getByRole("combobox", { name: "Demo persona" }).selectOption("admin.demo");
  await nav(page, "Patients");
  await expect(page.getByRole("alert").first()).toBeVisible();
});
