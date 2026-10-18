import { test } from "node:test";
import assert from "node:assert/strict";
import { release } from "../scripts/release.mjs";

const previous = "12345678-1234-1234-1234-123456789abc";
function rig({ deployments = [{ created_on: "2026-10-17", versions: [{ version_id: previous }] }], fail = () => false } = {}) {
  const commands = [], receipts = [];
  return { commands, receipts, options: {
    full: true,
    sh(cmd) { if (cmd.includes("status")) return ""; if (cmd.includes("rev-parse")) return "abc1234"; return JSON.stringify(deployments); },
    run(cmd) { commands.push(cmd); if (fail(cmd)) throw new Error(`Failed: ${cmd}`); },
    record(name, value) { receipts.push({ name, ...value }); }, delay: async () => {},
  } };
}

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
