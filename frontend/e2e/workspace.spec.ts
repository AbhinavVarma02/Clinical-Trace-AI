import { expect, test, type Page } from "@playwright/test";
import { DEMOS } from "../workspace/demos";

test("daily allowance disables execution, preserves browsing, and resets", async ({ page }) => {
  let exhausted = true;
  let starts = 0;
  page.on("request", r => { if (r.url().endsWith("/agent/runs/stream")) starts++; });
  await page.route("**/edge/status", route => route.fulfill({ json: {
    edge: { status: "ok", release: "quota-test", served_at: new Date().toISOString() },
    backend: { state: "ready", checked_at: new Date().toISOString(), latency_ms: 1, inference_mode: "llm" },
    inference: { quota_state: "available", configured: true },
    limits: { agent_runs_per_visitor_per_day: 25, remaining_visitor_agent_runs_today: exhausted ? 0 : 25,
      remaining_agent_runs_today: 200, resets_at: new Date(Date.now() + 60_000).toISOString() },
  } }));
  await page.goto("/app/");
  await expect(page.getByRole("button", { name: "Try a guided case", exact: true })).toBeDisabled();
  await expect(page.getByText(/Your connection's daily agent-run allowance is used up/)).toBeVisible();
  await nav(page, "Patients");
  await expect(page.getByRole("heading", { level: 1, name: "Patients" })).toBeVisible();
  await nav(page, "Case Review Agent");
  await page.getByRole("textbox", { name: "Question", exact: true }).fill("Why was metformin stopped?");
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeDisabled();
  await page.getByRole("textbox", { name: "Question", exact: true }).press("Enter");
  expect(starts).toBe(0);
  exhausted = false;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByRole("article", { name: "Answer to: Why was metformin stopped?" })).toBeVisible();
  expect(starts).toBe(1);
});

test("temporary run limit pauses retries and recovers without replaying the request", async ({ page }) => {
  await page.route("**/edge/status", route => route.fulfill({ json: {
    edge: { status: "ok", release: "quota-test", served_at: new Date().toISOString() },
    backend: { state: "ready", checked_at: new Date().toISOString(), latency_ms: 1 },
    inference: { quota_state: "available" }, limits: { remaining_agent_runs_today: 200 },
  } }));
  let starts = 0;
  await page.route("**/api/v2/agent/runs/stream", route => {
    starts++;
    return route.fulfill({ status: 429, headers: { "retry-after": "2" }, json: {
      error: { code: "rate_limited", message: "Temporary agent-run limit", scope: "visitor", limit_kind: "agent_runs", retry_after_s: 2 },
    } });
  });
  await page.goto("/app/");
  await nav(page, "Case Review Agent");
  await page.getByRole("textbox", { name: "Question", exact: true }).fill("Why was metformin stopped?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Try again", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeEnabled({ timeout: 5000 });
  expect(starts).toBe(1);
});

async function nav(page: Page, name: string) {
  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("navigation").getByRole("button", { name, exact: true }).click();
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/");
  await expect(page.getByRole("button", { name: "Try a guided case", exact: true })).toBeVisible();
});

for (const demo of DEMOS) {
  test(`guided scenario: ${demo.title}`, async ({ page }) => {
    // Chromium does not expose consumed SSE bodies through Network.getResponseBody.
    // Observe a clone of the real fetch response without replacing its data or timing.
    await page.evaluate(() => {
      const observed = window as typeof window & { __streamText?: string };
      const original = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        const response = await original(input, init);
        if (String(input).endsWith("/api/v2/agent/runs/stream")) {
          void response.clone().text().then(text => { observed.__streamText = text; });
        }
        return response;
      };
    });
    const response = page.waitForResponse(r => r.url().endsWith("/api/v2/agent/runs/stream") && r.request().method() === "POST");
    await page.getByRole("button", { name: new RegExp(demo.title) }).click();
    const http = await response;
    expect(http.ok()).toBeTruthy();
    await expect.poll(() => page.evaluate(() => (window as typeof window & { __streamText?: string }).__streamText ?? "")).toContain("event: result");
    const frames = (await page.evaluate(() => (window as typeof window & { __streamText: string }).__streamText)).split("\n\n");
    expect(frames.some(frame => frame.startsWith("event: progress"))).toBeTruthy();
    const final = frames.find(frame => frame.startsWith("event: result"));
    expect(final).toBeTruthy();
    const run = JSON.parse(final!.split("\ndata: ")[1]!);
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
    await expect(drawer.getByRole("button", { name: "Close panel" })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(drawer.getByRole("button", { name: "Close panel" })).toBeFocused();
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

test("responsive navigation, keyboard focus, persisted themes, and real errors", async ({ page }, testInfo) => {
  await page.screenshot({ path: testInfo.outputPath("overview-light.png"), fullPage: true, animations: "disabled" });
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.getByRole("button", { name: "Switch to dark theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: testInfo.outputPath("overview-dark.png"), fullPage: true, animations: "disabled" });
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await nav(page, "Evaluation");
  await expect(page.getByRole("heading", { name: /Evaluation/ }).first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  expect(await page.evaluate(() => { const bar = document.querySelector(".topbar")!; return bar.scrollWidth <= bar.clientWidth; })).toBeTruthy();
  // Administrator has ops access but no clinical-data access: exercise the real error state.
  await page.getByRole("combobox", { name: "Demo persona" }).selectOption("admin.demo");
  await nav(page, "Patients");
  await expect(page.getByRole("alert").first()).toBeVisible();
});

test("guided case: question to cited, approved artifact in four real steps", async ({ page }) => {
  await page.getByRole("button", { name: "Try a guided case", exact: true }).click();
  const answer = page.getByRole("article", { name: "Answer to: Summarize this patient journey" });
  await expect(answer.getByRole("list", { name: "Verified claims" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Guided case progress" })).toContainText("step 2 of 4");
  await answer.getByRole("button", { name: /^Open evidence / }).first().click();
  await expect(page.getByRole("dialog").getByText("Evidence text given to the verifier")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("region", { name: "Guided case progress" })).toContainText("step 3 of 4");
  await answer.getByRole("button", { name: "Submit for human review", exact: true }).click();
  await expect(page.getByText(/Submitted as review/)).toBeVisible();
  await page.getByRole("region", { name: "Guided case progress" }).getByRole("button", { name: "Switch to reviewer" }).click();
  await expect(page.getByRole("combobox", { name: "Demo persona" })).toHaveValue("reviewer.demo");
  await expect(page.getByText("Summarize this patient journey", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByText("Approval is valid for the exact artifact version shown.")).toBeVisible();
  await expect(page.getByRole("region", { name: "Guided case progress" })).toHaveCount(0);
  await nav(page, "Overview");
  await expect(page.getByRole("button", { name: "Run it again" })).toBeVisible();
});

test("cold start: queued question is sent when the edge reports the backend ready", async ({ page }) => {
  // Emulate the public edge in front of the real backend: /edge/status reports "waking" and API
  // calls get the edge's typed 503 until the backend is declared ready; then requests pass through.
  let ready = false;
  const status = (state: string) => ({
    edge: { status: "ok", release: "e2e", served_at: new Date().toISOString() },
    backend: { state, latency_ms: state === "ready" ? 20 : null, inference_mode: "deterministic", checked_at: new Date().toISOString() },
    inference: { provider: "cloudflare-workers-ai", model: "@cf/qwen/qwen3-30b-a3b-fp8", configured: true, daily_neuron_budget: 8000,
      neurons_used_today: 0, remaining_fraction: 1, quota_state: "available", resets_in_s: 3600 },
    limits: { agent_runs_today: 0, agent_runs_global_per_day: 300, agent_runs_per_visitor_per_day: 25, remaining_agent_runs_today: 300 },
  });
  await page.route("**/edge/status", route => route.fulfill({ json: status(ready ? "ready" : "waking") }));
  await page.route("**/api/v2/**", route => ready ? route.continue() : route.fulfill({
    status: 503, json: { error: { code: "backend_waking", message: "The analysis backend is starting up after being idle.", request_id: "e2e" } },
  }));
  await page.goto("/app/");
  await expect(page.getByText(/Starting the analysis backend/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Measured results" })).toBeVisible();
  await page.getByRole("button", { name: "Try a guided case", exact: true }).click();
  await expect(page.getByText(/Waiting for the analysis backend to start/)).toBeVisible();
  ready = true;
  const answer = page.getByRole("article", { name: "Answer to: Summarize this patient journey" });
  await expect(answer.getByRole("list", { name: "Verified claims" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/Starting the analysis backend/)).toHaveCount(0);
});
