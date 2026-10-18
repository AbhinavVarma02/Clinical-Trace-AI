// Public smoke test for a deployed edge, run as an unauthenticated external visitor.
//
//   node scripts/smoke.mjs https://clinical-trace-ai.<subdomain>.workers.dev [--release <sha>] [--full] [--out file.json]
//
// Baseline checks always run (static app, security headers, edge status/metrics, route allow-list,
// inference-proxy auth). --full additionally requires a ready backend and executes the complete
// guided workflow through the public URL: patient list, streamed agent run with real tool calls,
// citation resolution, review submission and approval by a second persona in the same workspace.
import { randomBytes } from "node:crypto";
import { writeFileSync } from "node:fs";

const args = process.argv.slice(2);
const base = (args.find((a) => a.startsWith("http")) ?? "").replace(/\/$/, "");
const flag = (name) => args.includes(name);
const option = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
if (!base) {
  console.error("usage: node scripts/smoke.mjs <base-url> [--release sha] [--full] [--out file]");
  process.exit(2);
}

const results = [];
const workspace = randomBytes(16).toString("hex");
async function check(name, fn) {
  const started = performance.now();
  try {
    const detail = await fn();
    results.push({ name, ok: true, ms: Math.round(performance.now() - started), ...(detail ? { detail } : {}) });
  } catch (err) {
    results.push({ name, ok: false, ms: Math.round(performance.now() - started), error: String(err?.message ?? err) });
  }
}
function assert(condition, message) { if (!condition) throw new Error(message); }
const api = (persona) => ({ "X-Demo-Persona": persona, "X-Demo-Workspace": workspace, "Content-Type": "application/json" });

await check("root redirects to the workspace", async () => {
  const r = await fetch(`${base}/`, { redirect: "manual" });
  assert(r.status === 302 && (r.headers.get("location") ?? "").endsWith("/app/"), `got ${r.status} ${r.headers.get("location")}`);
});
await check("workspace HTML with security headers", async () => {
  const r = await fetch(`${base}/app/`);
  const html = await r.text();
  assert(r.ok && html.includes('<div id="root">'), `status ${r.status}`);
  const csp = r.headers.get("content-security-policy") ?? "";
  assert(csp.includes("default-src 'self'") && csp.includes("frame-ancestors 'none'"), "CSP missing");
  assert(r.headers.get("x-content-type-options") === "nosniff", "nosniff missing");
  const asset = html.match(/\/app\/assets\/[^"]+\.js/)?.[0];
  assert(asset, "no script asset referenced");
  const js = await fetch(`${base}${asset}`);
  assert(js.ok && (js.headers.get("cache-control") ?? "").includes("immutable"), "asset not cacheable");
  return { html_bytes: html.length, asset };
});
let status;
await check("edge status", async () => {
  const r = await fetch(`${base}/edge/status`);
  status = await r.json();
  assert(r.ok && status.edge?.status === "ok", "edge not ok");
  const release = option("--release");
  if (release) assert(status.edge.release === release, `release ${status.edge.release} != ${release}`);
  return { release: status.edge.release, backend: status.backend.state, inference: status.inference.quota_state };
});
await check("edge metrics", async () => {
  const r = await fetch(`${base}/edge/metrics`);
  const body = await r.json();
  assert(r.ok && body.requests && body.inference, "metrics malformed");
});
await check("unlisted routes are refused", async () => {
  for (const path of ["/api/v1/predict", "/api/v2/admin", "/fhir/metadata", "/edge/unknown"]) {
    const r = await fetch(`${base}${path}`);
    assert(r.status === 404, `${path} returned ${r.status}`);
  }
});
await check("inference proxy requires the backend token", async () => {
  const r = await fetch(`${base}/edge/llm/v1/chat/completions`, { method: "POST", body: "{}" });
  assert(r.status === 401, `got ${r.status}`);
});
await check("malformed workspace is rejected at the edge", async () => {
  const r = await fetch(`${base}/api/v2/patients`, { headers: { "X-Demo-Persona": "clinician.demo", "X-Demo-Workspace": "bad id" } });
  assert(r.status === 400, `got ${r.status}`);
});

if (flag("--full")) {
  let run;
  let review;
  await check("backend ready (waits through a cold start, up to 5 min)", async () => {
    const started = Date.now();
    let state = status?.backend?.state;
    while (state !== "ready") {
      assert(state !== "not_configured", "no backend configured");
      assert(Date.now() - started < 300_000, `backend still ${state}`);
      await new Promise((r) => setTimeout(r, 5000));
      state = (await (await fetch(`${base}/edge/status`)).json()).backend.state;
    }
    return { waited_ms: Date.now() - started };
  });
  await check("patients listed through the gateway", async () => {
    const r = await fetch(`${base}/api/v2/patients`, { headers: api("clinician.demo") });
    const body = await r.json();
    assert(r.ok && body.length === 4, `status ${r.status}`);
  });
  await check("streamed agent run with real tool calls", async () => {
    const started = performance.now();
    const r = await fetch(`${base}/api/v2/agent/runs/stream`, { method: "POST", headers: api("clinician.demo"),
      body: JSON.stringify({ patient_id: "synthetic_p001", question: "Summarize this patient journey" }) });
    assert(r.ok && (r.headers.get("content-type") ?? "").includes("text/event-stream"), `status ${r.status}`);
    const text = await r.text();
    const frames = text.split("\n\n");
    const result = frames.find((f) => f.startsWith("event: result"));
    assert(result, "no result frame");
    run = JSON.parse(result.split("\ndata: ")[1]);
    assert(run.outcome === "answered", `outcome ${run.outcome}`);
    assert(run.tool_calls.length > 0 && run.answer.claims.length > 0, "no tools or claims");
    assert(run.inference?.mode === "llm" && !run.inference?.degraded_reason && !run.inference?.fallback_used
      && run.inference?.composer?.startsWith("llm") && run.inference?.planner === "llm",
      `full release gate requires real LLM planning/composition; got ${run.inference?.planner}/${run.inference?.composer}/${run.inference?.degraded_reason ?? "no degradation"}`);
    return { completion_ms: Math.round(performance.now() - started), progress_frames: frames.filter((f) => f.startsWith("event: progress")).length,
      tool_calls: run.tool_calls.length, claims: run.answer.claims.length, inference: run.inference.mode, model: run.inference.provider?.model ?? null,
      degraded: run.inference.degraded_reason ?? null, planner: run.inference.planner };
  });
  await check("every citation resolves to versioned evidence of the same patient", async () => {
    const ids = new Set(run.evidence.map((e) => e.evidence_id));
    for (const claim of run.answer.claims) for (const c of claim.citations) assert(ids.has(c), `dangling citation ${c}`);
    assert(run.evidence.every((e) => e.patient_id === "synthetic_p001" && e.source_version), "evidence isolation/version");
  });
  await check("another visitor cannot see this run", async () => {
    const other = { ...api("clinician.demo"), "X-Demo-Workspace": randomBytes(16).toString("hex") };
    const r = await fetch(`${base}/api/v2/agent/runs/${run.run_id}`, { headers: other });
    assert(r.status === 404, `got ${r.status}`);
  });
  await check("submit for review", async () => {
    const r = await fetch(`${base}/api/v2/reviews`, { method: "POST", headers: api("clinician.demo"), body: JSON.stringify({ run_id: run.run_id }) });
    review = await r.json();
    assert(r.ok && review.status === "pending_review", `status ${r.status}`);
  });
  await check("reviewer approves the exact artifact hash", async () => {
    const r = await fetch(`${base}/api/v2/reviews/${review.review_id}/decision`, { method: "POST", headers: api("reviewer.demo"),
      body: JSON.stringify({ decision: "approved", artifact_hash: review.artifact_hash }) });
    const body = await r.json();
    assert(r.ok && body.status === "approved" && body.approval_valid === true, `status ${r.status}`);
  });
}

const report = { base, at: new Date().toISOString(), full: flag("--full"), passed: results.every((r) => r.ok), results };
console.log(JSON.stringify(report, null, 1));
const out = option("--out");
if (out) writeFileSync(out, JSON.stringify(report, null, 1) + "\n");
process.exit(report.passed ? 0 : 1);
