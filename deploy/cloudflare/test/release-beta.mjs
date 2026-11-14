import { after, test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assemble, BETA_URL, checkCopy, forbiddenReferences, releaseBeta, smoke } from "../scripts/release-beta.mjs";

// Every value below is invented; no Epic data is involved, and nothing is written to the real deploy folder.
const account = "00f876c0ebbf368d25feabe3e49cbc0b";
const head = "abc1234def5678abc1234def5678abc1234def56";
const previous = "12345678-1234-1234-1234-123456789abc";
const realSite = join(dirname(fileURLToPath(import.meta.url)), "..", "beta", "public");
const temporary = [];
after(() => temporary.forEach(dir => rmSync(dir, { recursive: true, force: true })));
const section = items => ({ items, total: items.length, truncated: false });

function copy(change = () => {}) {
  const gid = "a".repeat(32);
  const document = { format: "clinical-trace-epic-beta-publication/1", exported_at: 1, withheld: ["attempt_id"], view: {
    mode: "LIVE", data_origin: "epic_sandbox", label: "Approved LIVE patient", source: "epic-fhir-sandbox-r4",
    publication: "published_snapshot", completeness: "unconfirmed", serving: "stale", freshness: "stale", evidence_error: null,
    active: { generation_id: gid, retrieved_at: 1, promoted_at: 2 },
    provenance: { generation_id: gid, mode: "LIVE", source: "epic-fhir-sandbox-r4", synthetic_transport: false, manifest_sha256: "c".repeat(64), bundle_sha256: "b".repeat(64) },
    record: { patient: { display_name: "Invented" }, conditions: section([]), encounters: section([]), labs: section([]), vitals: section([]), reports: section([]),
      notes: section([{ title: "Invented note", source: { synthetic: false } }]) },
    warnings: [{ code: "published_snapshot", severity: "warning", message: "Invented." }],
  } };
  change(document);
  const bytes = Buffer.from(JSON.stringify(document));
  return { bytes, sha256: createHash("sha256").update(bytes).digest("hex") };
}
const app = [{ path: "beta/app/index.html", text: '<div id="root"></div>' }, { path: "beta/app/assets/index.js", text: "fetch(BASE+'published/record.json')" }];
function rig({ branch = "feature/beta", tip = head, document = copy(), files = app, deployments = [{ created_on: "2026-11-20", versions: [{ version_id: previous }] }],
  listFails = false, firstDeploy = false, fail = () => false, probeFailures = 0 } = {}) {
  const commands = [], receipts = [], remote = [], probes = [];
  const site = mkdtempSync(join(tmpdir(), "beta-site-"));
  temporary.push(site);
  return { commands, receipts, remote, probes, site, options: {
    copy: "copy.json", sha256: document.sha256, firstDeploy, account, site, readCopy: () => document.bytes, published: () => files, clear: () => {},
    sh(cmd) {
      if (cmd.includes("branch --show-current")) return branch;
      if (cmd.includes("status")) return "";
      if (cmd.includes("ls-remote")) return `${tip}\trefs/heads/${branch}\n`;
      if (cmd.includes("rev-parse --short")) return "abc1234";
      if (cmd.includes("rev-parse")) return head;
      if (cmd.includes("whoami")) return JSON.stringify({ accounts: [{ id: account }] });
      remote.push(cmd);
      if (listFails) throw new Error("Worker not found");
      return JSON.stringify(deployments);
    },
    run(cmd, cwd, env) { commands.push({ cmd, env }); if (fail(cmd)) throw new Error(`Failed: ${cmd}`); },
    record(name, value) { receipts.push({ name, ...value }); }, delay: async () => {},
    probe: async (url, sha) => { probes.push([url, sha]); if (probes.length <= probeFailures) throw new Error("Beta smoke failed"); },
  } };
}

test("only approved bytes of a fully consistent Epic sandbox copy are accepted", () => {
  const good = copy();
  assert.equal(checkCopy(good.bytes, good.sha256).view.data_origin, "epic_sandbox");
  assert.throws(() => checkCopy(good.bytes, "0".repeat(64)), /approved SHA-256/);
  for (const change of [
    d => { d.view.data_origin = "synthetic_live_fixture"; d.view.provenance.synthetic_transport = true; },
    d => { d.view.label = "Synthetic LIVE-shaped fixture"; },
    d => { d.view.warnings.push({ code: "synthetic_live_fixture" }); },
    d => { d.view.record.notes.items[0].source.synthetic = true; },
    d => { d.view.provenance.attempt_id = "x".repeat(32); }, d => { d.withheld = []; }, d => { d.format = "other"; },
    d => { d.view.publication = "disabled"; }, d => { d.view.mode = "MOCK"; }, d => { d.view.evidence_error = "generation_integrity"; },
    d => { d.view.serving = "fresh"; }, d => { d.view.provenance.generation_id = "f".repeat(32); },
    d => { d.view.provenance.bundle_sha256 = "short"; }, d => { delete d.view.record.labs; }, d => { d.view.warnings = []; },
  ]) {
    const bad = copy(change);
    assert.throws(() => checkCopy(bad.bytes, bad.sha256), /Only an approved Epic sandbox publication copy/);
  }
});

test("a Beta release from another branch or an unpublished commit stops before any build or remote step", async () => {
  for (const options of [{ branch: "main" }, { tip: "0".repeat(40) }]) {
    const r = rig(options);
    await assert.rejects(releaseBeta(r.options), /released only from feature\/beta|not the published tip/);
    assert.deepEqual([r.commands, r.remote, r.receipts], [[], [], []]);
  }
});

test("a synthetic or unapproved copy stops before any build or remote step", async () => {
  const r = rig({ document: copy(d => { d.view.data_origin = "synthetic_live_fixture"; d.view.provenance.synthetic_transport = true; }) });
  await assert.rejects(releaseBeta(r.options), /Only an approved Epic sandbox publication copy/);
  assert.deepEqual([r.commands, r.remote, r.receipts], [[], [], []]);
});

test("owner code or a loopback address in the build stops before any remote step", async () => {
  for (const text of ["fetch('/owner/genuine/read/options')", "navigator.credentials.get(x)", "https://owner.clinical-trace.localhost:8443/"]) {
    const r = rig({ files: [...app, { path: "beta/app/assets/x.js", text }] });
    await assert.rejects(releaseBeta(r.options), /loopback addresses or owner code in: beta\/app\/assets\/x\.js/);
    assert.equal(r.commands.length, 1);
    assert.deepEqual([r.remote, r.receipts], [[], []]);
  }
});

test("the published build refuses fixtures, deploys only the Beta Worker and never touches the real deploy folder", async () => {
  const realBefore = existsSync(realSite);
  const r = rig({ listFails: true, firstDeploy: true });
  await releaseBeta(r.options);
  assert.match(r.commands[0].cmd, /vite build --config vite\.beta\.config\.ts/);
  assert.deepEqual(r.commands[0].env, { VITE_BETA_CLIENT: "published", VITE_BETA_ALLOW_FIXTURE: "false" });
  assert.deepEqual(r.commands.slice(1).map(c => c.cmd), ["npx wrangler deploy --config beta/wrangler.jsonc"]);
  assert.deepEqual(r.probes, [[BETA_URL, r.options.sha256]]);
  assert.equal(r.receipts.at(-1).state, "passed");
  assert.equal(r.receipts[0].copy_sha256, r.options.sha256);
  assert(existsSync(join(r.site, "beta", "app", "published", "record.json")));
  assert.equal(existsSync(realSite), realBefore);
});

test("a slow first answer is retried; a persistent smoke failure rolls back, and a failed first deployment waits for an operator", async () => {
  const slow = rig({ probeFailures: 2 });
  await releaseBeta(slow.options);
  assert.equal(slow.probes.length, 3);
  assert.equal(slow.receipts.at(-1).state, "passed");
  const r = rig({ probeFailures: 99 });
  await assert.rejects(releaseBeta(r.options), /Beta smoke failed/);
  assert.equal(r.probes.length, 6);
  assert.match(r.commands.at(-1).cmd, new RegExp(`^npx wrangler rollback ${previous} --config beta/wrangler\\.jsonc`));
  assert.equal(r.receipts.at(-1).state, "rolled_back");
  const first = rig({ listFails: true, firstDeploy: true, probeFailures: 99 });
  await assert.rejects(releaseBeta(first.options), /operator action required/);
  assert(!first.commands.some(c => /rollback|delete/.test(c.cmd)));
  assert.equal(first.receipts.at(-1).state, "first_deploy_failed");
});

test("assembly adds the copy, strict headers, redirects and drops the build manifest", () => {
  const dir = mkdtempSync(join(tmpdir(), "beta-assets-"));
  temporary.push(dir);
  const { bytes } = copy();
  assemble(dir, bytes);
  assert.deepEqual(readFileSync(join(dir, "beta", "app", "published", "record.json")), bytes);
  const headers = readFileSync(join(dir, "_headers"), "utf8");
  for (const expected of ["default-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "X-Robots-Tag: noindex", "publickey-credentials-get=()", "Cache-Control: no-store"]) {
    assert(headers.includes(expected), expected);
  }
  assert.match(readFileSync(join(dir, "_redirects"), "utf8"), /^\/ \/beta\/app\/ 302$/m);
  assert.equal(readFileSync(join(dir, ".assetsignore"), "utf8"), "beta/app/.vite\n");
});

test("the code scan covers every file except the hash-approved copy", () => {
  const copyWithText = { path: "beta/app/published/record.json", text: "note mentions localhost and /owner/ in clinical text" };
  assert.deepEqual(forbiddenReferences([...app, copyWithText]), []);
  assert.deepEqual(forbiddenReferences([{ path: "beta/app/assets/a.js", text: "http://127.0.0.1:8000" }]), ["beta/app/assets/a.js"]);
});

test("smoke requires the app, its security headers and exactly the approved copy", async () => {
  const { bytes, sha256 } = copy();
  const site = (headers, served = bytes) => async url => url.endsWith("/beta/app/")
    ? { status: 200, text: async () => '<div id="root"></div>', headers: new Headers(headers) }
    : { status: 200, arrayBuffer: async () => served, headers: new Headers() };
  const secure = { "content-security-policy": "default-src 'self'", "x-robots-tag": "noindex, nofollow" };
  await smoke(BETA_URL, sha256, site(secure));
  await assert.rejects(smoke(BETA_URL, sha256, site({})), /security headers/);
  await assert.rejects(smoke(BETA_URL, sha256, site(secure, Buffer.from("{}"))), /differs from the approved copy/);
});
