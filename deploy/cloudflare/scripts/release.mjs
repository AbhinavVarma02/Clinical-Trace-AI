// Release the edge: fresh workspace build -> checks -> deploy -> public smoke -> automatic rollback.
//
//   node scripts/release.mjs [--full]
//
// Refuses to release uncommitted workspace/edge sources so every deployment maps to a commit.
// The previous Worker version is recorded before deploying; if the smoke test fails, the script
// rolls back to it with `wrangler rollback`. Rollback by hand:
//   npx wrangler deployments list        # find the previous version id
//   npx wrangler rollback <version-id> --message "manual rollback"
import { execSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(here, "..", "..");
const sh = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: ["ignore", "pipe", "inherit"], encoding: "utf8" });
const run = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: "inherit" });

const dirty = sh("git status --porcelain -- frontend/workspace frontend/package.json deploy/cloudflare/src deploy/cloudflare/wrangler.jsonc deploy/cloudflare/scripts", repo).trim();
if (dirty) {
  console.error(`Refusing to release uncommitted sources:\n${dirty}`);
  process.exit(1);
}
const sha = sh("git rev-parse --short HEAD", repo).trim();
const url = "https://clinical-trace-ai.room-for-both.workers.dev";

run("npm run build:workspace", join(repo, "frontend"));
run("npm run check");
run("npm run build:assets");

let previous = null;
try {
  const versions = JSON.parse(sh("npx wrangler deployments list --json"));
  versions.sort((a, b) => String(a.created_on).localeCompare(String(b.created_on)));
  previous = versions.at(-1)?.versions?.[0]?.version_id ?? null;
} catch {
  console.warn("Could not read the current deployment; automatic rollback will be unavailable.");
}
// Schema first: a Worker must never run against an unmigrated database.
run("npx wrangler d1 migrations apply clinical-trace-edge --remote");
run(`npx wrangler deploy --var RELEASE:${sha}`);
mkdirSync(join(here, ".wrangler"), { recursive: true });
writeFileSync(join(here, ".wrangler", "last-release.json"), JSON.stringify({ sha, previous, at: new Date().toISOString() }, null, 1));

const out = join(here, ".wrangler", `smoke-${sha}.json`);
try {
  // Edge propagation is usually immediate; allow a few seconds before asserting the release.
  await new Promise((r) => setTimeout(r, 5000));
  run(`node scripts/smoke.mjs ${url} --release ${sha} --out "${out}"${process.argv.includes("--full") ? " --full" : ""}`);
  console.log(`Released ${sha}; smoke report ${out}`);
} catch {
  if (previous) {
    console.error(`Smoke test failed; rolling back to ${previous}`);
    run(`npx wrangler rollback ${previous} --message "automatic rollback: smoke failed for ${sha}" -y`);
  }
  process.exit(1);
}
