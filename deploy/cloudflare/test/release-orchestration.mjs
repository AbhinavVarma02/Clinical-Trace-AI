import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BETA_PUBLIC_URL, betaLinkSources, loopbackReferences, pinnedAccount, readTree, release } from "../scripts/release.mjs";

const previous = "12345678-1234-1234-1234-123456789abc";
const account = "00f876c0ebbf368d25feabe3e49cbc0b";
const head = "abc1234def5678abc1234def5678abc1234def56";
const localBeta = "https://owner.clinical-trace.localhost:8443/beta/app/";
function rig({ deployments = [{ created_on: "2026-10-17", versions: [{ version_id: previous }] }], fail = () => false,
  visible = [account], listFails = false, firstDeploy = false, branch = "main", tip = head, lsRemote = null, betaLink = [], withBetaLink = false,
  published = [{ path: "app/index.html", text: '<a href="https://clinical-trace-ai.vathadiabhinav.workers.dev/app/">' }] } = {}) {
  const commands = [], receipts = [], remote = [], envs = [];
  return { commands, receipts, remote, envs, options: {
    full: true, firstDeploy, account, withBetaLink, betaLink: () => betaLink, published: () => published,
    sh(cmd) {
      if (cmd.includes("branch --show-current")) return branch;
      if (cmd.includes("status")) return "";
      if (cmd.includes("ls-remote")) return lsRemote ?? (tip ? `${tip}\trefs/heads/${branch}\n` : "");
      if (cmd.includes("rev-parse --short")) return "abc1234";
      if (cmd.includes("rev-parse")) return head;
      if (cmd.includes("deployments list")) remote.push(cmd);
      if (cmd.includes("whoami")) return `banner\n${JSON.stringify({ accounts: visible.map(id => ({ id })) })}`;
      if (listFails) throw new Error("Worker not found");
      return JSON.stringify(deployments);
    },
    run(cmd, cwd, env) { commands.push(cmd); envs.push(env); if (fail(cmd)) throw new Error(`Failed: ${cmd}`); },
    record(name, value) { receipts.push({ name, ...value }); }, delay: async () => {},
  } };
}

test("account pin is read from the configuration and required", () => {
  assert.equal(pinnedAccount(`{ "name": "x", "account_id": "${account}" }`), account);
  assert.throws(() => pinnedAccount(`{ "name": "x" }`), /no pinned account_id/);
});

test("a release from any branch other than main stops before any build or remote step", async () => {
  for (const branch of ["feature/beta", ""]) {
    const r = rig({ branch });
    await assert.rejects(release(r.options), /released only from main/);
    assert.deepEqual(r.commands, []);
    assert.deepEqual(r.receipts, []);
  }
});

test("a commit that is not the published tip of the release branch stops before any build or remote step", async () => {
  for (const tip of ["0000000000000000000000000000000000000000", ""]) {
    const r = rig({ tip });
    await assert.rejects(release(r.options), /not the published tip of origin\/main/);
    assert.deepEqual(r.commands, []);
    assert.deepEqual(r.receipts, []);
  }
});

test("only the exact release branch ref counts as its published tip", async () => {
  const decoy = `${head}\trefs/heads/a/refs/heads/main\n0000000000000000000000000000000000000000\trefs/heads/main\n`;
  const r = rig({ lsRemote: decoy });
  await assert.rejects(release(r.options), /not the published tip of origin\/main/);
  assert.deepEqual(r.commands, []);
  const ok = rig({ lsRemote: `1111111111111111111111111111111111111111\trefs/heads/a/refs/heads/main\n${head}\trefs/heads/main\n` });
  await release(ok.options);
  assert.equal(ok.receipts.at(-1).state, "passed");
});

test("a release with the local Beta link flag set stops before any build or remote step", async () => {
  const r = rig({ betaLink: ["frontend/workspace/.env.local"] });
  await assert.rejects(release(r.options), /set in frontend\/workspace\/\.env\.local; use --with-beta-link instead/);
  assert.deepEqual(r.commands, []);
  assert.deepEqual(r.receipts, []);
});

test("the Beta link flag is found in the environment and in the git-ignored files vite build reads", () => {
  const none = () => null;
  assert.deepEqual(betaLinkSources({}, none), []);
  assert.deepEqual(betaLinkSources({ VITE_ENABLE_BETA_LINK: "true" }, none), ["the environment"]);
  const files = { ".env": "OTHER=1\r\nVITE_ENABLE_BETA_LINKS=true\r\n", ".env.local": "# note\r\nVITE_ENABLE_BETA_LINK=true\r\n",
    ".env.production": "export VITE_ENABLE_BETA_LINK = true", ".env.production.local": "VITE_ENABLE_BETA_LINK: 'true'",
    ".env.development": "VITE_ENABLE_BETA_LINK=true" };
  assert.deepEqual(betaLinkSources({}, name => files[name] ?? null),
    ["frontend/workspace/.env.local", "frontend/workspace/.env.production", "frontend/workspace/.env.production.local"]);
});

test("a built workspace that still carries the local Beta link stops before any remote step", async () => {
  const r = rig({ published: [{ path: "_headers", text: "/*" }, { path: "app/assets/index-x.js", text: `href:\`${localBeta}\`` }] });
  await assert.rejects(release(r.options), /loopback-only addresses .* in: app\/assets\/index-x\.js$/);
  assert.deepEqual(r.commands, ["npm run build:workspace", "npm run check", "npm run build:assets"]);
  assert.deepEqual(r.remote, []);
  assert.deepEqual(r.receipts, []);
});

test("the hosted Beta link ships only when the release asks for it, and then must be in the build", async () => {
  const withLink = [{ path: "app/assets/index.js", text: `href:\`${BETA_PUBLIC_URL}\`` }];
  const asked = rig({ withBetaLink: true, published: withLink });
  await release(asked.options);
  assert.deepEqual(asked.envs[0], { VITE_ENABLE_BETA_LINK: "true" });
  assert.equal(asked.receipts.at(-1).state, "passed");
  const plain = rig();
  await release(plain.options);
  assert.deepEqual(plain.envs[0], { VITE_ENABLE_BETA_LINK: "false" });
  for (const [options, message] of [[{ withBetaLink: true }, /lacks the hosted Beta link/], [{ published: withLink }, /did not ask for it/]]) {
    const r = rig(options);
    await assert.rejects(release(r.options), message);
    assert.deepEqual(r.commands, ["npm run build:workspace", "npm run check", "npm run build:assets"]);
    assert.deepEqual(r.remote, []);
    assert.deepEqual(r.receipts, []);
  }
});

test("loopback and *.localhost addresses are found, public ones are not", () => {
  const hits = ["https://owner.clinical-trace.localhost:8443/beta/app/", "http://LOCALHOST:3002", "http://127.0.0.1:8000/api", "http://[::1]:8443", "0.0.0.0:8000",
    "http://[0:0:0:0:0:0:0:1]/", "http://[::]:80", "http://[::ffff:7f00:1]/", "http://[::ffff:127.0.0.1]/"];
  const clean = ["https://clinical-trace-ai.vathadiabhinav.workers.dev/app/", "https://example.org/localhostname", "v1.127.0.10"];
  assert.deepEqual(loopbackReferences(hits.map((text, i) => ({ path: `hit${i}`, text }))), hits.map((_, i) => `hit${i}`));
  assert.deepEqual(loopbackReferences(clean.map((text, i) => ({ path: `clean${i}`, text }))), []);
});

test("the published tree is read recursively with forward-slash paths", () => {
  const dir = mkdtempSync(join(tmpdir(), "release-tree-"));
  try {
    mkdirSync(join(dir, "app", "assets"), { recursive: true });
    writeFileSync(join(dir, "_headers"), "/*");
    writeFileSync(join(dir, "app", "assets", "index.js"), localBeta);
    assert.deepEqual(readTree(dir).map(file => file.path).sort(), ["_headers", "app/assets/index.js"]);
    assert.deepEqual(loopbackReferences(readTree(dir)), ["app/assets/index.js"]);
    // A link could point the upload at files the scan never read, so any link stops the release.
    symlinkSync(join(dir, "app"), join(dir, "linked"), "junction");
    assert.throws(() => readTree(dir), /links or special files: linked/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a login without the pinned account stops before any build or remote step", async () => {
  const r = rig({ visible: ["4d8c9b9dcf23e738359858eb1a6d9d9e"] });
  await assert.rejects(release(r.options), /cannot access pinned account/);
  assert.equal(r.commands.length, 0);
});

test("publication requires a known previous version before migrations", async () => {
  for (const deployments of [[], [{ versions: [{ version_id: previous }, { version_id: previous }] }], [{ versions: [{ version_id: "invalid" }] }]]) {
    const r = rig({ deployments });
    await assert.rejects(release(r.options), /previous Worker/);
    assert(!r.commands.some(cmd => /--remote|wrangler deploy --var/.test(cmd)));
  }
});

test("successful coordinated release requires full smoke and records previous version", async () => {
  const r = rig(); await release(r.options);
  assert(r.commands.find(cmd => cmd.includes("smoke.mjs")).endsWith(" --full"));
  assert.equal(r.receipts.at(-1).state, "passed");
  assert.equal(r.receipts[0].previous, previous);
  assert.equal(r.receipts[0].account, account);
  assert(!r.commands.some(cmd => cmd.includes("wrangler rollback")));
});

for (const failingStep of ["wrangler deploy --var", "smoke.mjs"]) {
  test(`${failingStep} failure rolls edge back and returns failure`, async () => {
    const r = rig({ fail: cmd => cmd.includes(failingStep) });
    await assert.rejects(release(r.options), /Failed/);
    assert(r.commands.at(-1).startsWith(`npx wrangler rollback ${previous}`));
    assert.equal(r.receipts.at(-1).state, "rolled_back");
  });
}

test("rollback failure is explicit and preserves its failed receipt", async () => {
  const r = rig({ fail: cmd => /smoke.mjs|wrangler rollback/.test(cmd) });
  await assert.rejects(release(r.options), /operator action required/);
  assert.equal(r.receipts.at(-1).state, "rollback_failed");
});

test("a failed migration stops before edge deploy, without claiming database rollback", async () => {
  const r = rig({ fail: cmd => cmd.includes("migrations apply") });
  await assert.rejects(release(r.options), /Failed/);
  assert(!r.commands.some(cmd => /wrangler deploy --var|wrangler rollback/.test(cmd)));
  assert.equal(r.receipts.at(-1).state, "migration_failed");
});

test("first deployment to a new account publishes without a previous version", async () => {
  const r = rig({ listFails: true, firstDeploy: true }); await release(r.options);
  assert.equal(r.receipts[0].previous, null);
  assert.equal(r.receipts.at(-1).state, "passed");
});

test("a secrets file is passed to the deploy step only", async () => {
  const r = rig({ listFails: true, firstDeploy: true }); await release({ ...r.options, secretsFile: ".prod-secrets.x.json" });
  assert(r.commands.find(cmd => cmd.startsWith("npx wrangler deploy")).endsWith(' --secrets-file ".prod-secrets.x.json"'));
  assert.equal(r.commands.filter(cmd => cmd.includes("secrets-file")).length, 1);
});

test("first deployment refuses an existing Worker", async () => {
  const r = rig({ firstDeploy: true });
  await assert.rejects(release(r.options), /already has deployments/);
  assert(!r.commands.some(cmd => /--remote|wrangler deploy --var/.test(cmd)));
});

test("failed first deployment never deletes or rolls back and needs an operator", async () => {
  const r = rig({ listFails: true, firstDeploy: true, fail: cmd => cmd.includes("smoke.mjs") });
  await assert.rejects(release(r.options), /operator action required/);
  assert(!r.commands.some(cmd => /wrangler (rollback|delete)/.test(cmd)));
  assert.equal(r.receipts.at(-1).state, "first_deploy_failed");
});

test("a normal release does not treat a failed deployment listing as a first deployment", async () => {
  const r = rig({ listFails: true });
  await assert.rejects(release(r.options), /not found/);
  assert(!r.commands.some(cmd => /--remote|wrangler deploy --var/.test(cmd)));
});
