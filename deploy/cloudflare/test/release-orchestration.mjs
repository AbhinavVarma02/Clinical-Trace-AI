import { test } from "node:test";
import assert from "node:assert/strict";
import { pinnedAccount, release } from "../scripts/release.mjs";

const previous = "12345678-1234-1234-1234-123456789abc";
const account = "00f876c0ebbf368d25feabe3e49cbc0b";
function rig({ deployments = [{ created_on: "2026-10-17", versions: [{ version_id: previous }] }], fail = () => false,
  visible = [account], listFails = false, firstDeploy = false } = {}) {
  const commands = [], receipts = [];
  return { commands, receipts, options: {
    full: true, firstDeploy, account,
    sh(cmd) {
      if (cmd.includes("status")) return "";
      if (cmd.includes("rev-parse")) return "abc1234";
      if (cmd.includes("whoami")) return `banner\n${JSON.stringify({ accounts: visible.map(id => ({ id })) })}`;
      if (listFails) throw new Error("Worker not found");
      return JSON.stringify(deployments);
    },
    run(cmd) { commands.push(cmd); if (fail(cmd)) throw new Error(`Failed: ${cmd}`); },
    record(name, value) { receipts.push({ name, ...value }); }, delay: async () => {},
  } };
}

test("account pin is read from the configuration and required", () => {
  assert.equal(pinnedAccount(`{ "name": "x", "account_id": "${account}" }`), account);
  assert.throws(() => pinnedAccount(`{ "name": "x" }`), /no pinned account_id/);
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
