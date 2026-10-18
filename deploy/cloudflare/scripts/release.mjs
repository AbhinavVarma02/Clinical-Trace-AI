// Coordinated edge release. Publication still requires explicit owner approval.
// A known previous Worker is required; deploy/smoke failure triggers edge rollback.
import { execSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(here, "..", "..");
const shell = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: ["ignore", "pipe", "inherit"], encoding: "utf8" });
const execute = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: "inherit" });
const save = (name, value) => {
  mkdirSync(join(here, ".wrangler"), { recursive: true });
  writeFileSync(join(here, ".wrangler", name), JSON.stringify(value, null, 1) + "\n");
};

export async function release({ full = false, sh = shell, run = execute, record = save,
  delay = () => new Promise(r => setTimeout(r, 5000)) } = {}) {
  const dirty = sh("git status --porcelain -- frontend deploy/cloudflare", repo).trim();
  if (dirty) throw new Error(`Refusing to release uncommitted sources:\n${dirty}`);
  const sha = sh("git rev-parse --short HEAD", repo).trim();
  if (!/^[0-9a-f]{7,40}$/.test(sha)) throw new Error("Cannot identify release commit");
  run("npm run build:workspace", join(repo, "frontend"));
  run("npm run check");
  run("npm run build:assets");

  // Fail before migrations/publication if rollback cannot be prepared. Do not
  // guess a previous version or roll back a deployment split across versions.
  const versions = JSON.parse(sh("npx wrangler deployments list --json"));
  versions.sort((a, b) => String(a.created_on).localeCompare(String(b.created_on)));
  const latest = versions.at(-1);
  const previous = latest?.versions?.length === 1 ? latest.versions[0]?.version_id : null;
  if (typeof previous !== "string" || !/^[0-9a-f-]{36}$/i.test(previous)) {
    throw new Error("No unambiguous previous Worker version; release stopped before publication");
  }
  const receipt = { sha, previous, at: new Date().toISOString(), state: "prepared" };
  record("last-release.json", receipt);
  // Database migrations have no automatic rollback. Only compatible additive
  // migrations belong here; review any destructive migration before release.
  try {
    run("npx wrangler d1 migrations apply clinical-trace-edge --remote");
  } catch (error) {
    record("last-release.json", { ...receipt, state: "migration_failed" });
    throw error;
  }
  try {
    run(`npx wrangler deploy --var RELEASE:${sha}`);
    await delay();
    const out = join(here, ".wrangler", `smoke-${sha}.json`);
    run(`node scripts/smoke.mjs https://clinical-trace-ai.room-for-both.workers.dev --release ${sha} --out "${out}"${full ? " --full" : ""}`);
    record("last-release.json", { ...receipt, state: "passed" });
    console.log(`Released ${sha}; smoke report ${out}`);
  } catch (error) {
    record("last-release.json", { ...receipt, state: "failed" });
    try {
      run(`npx wrangler rollback ${previous} --message "automatic rollback: release failed for ${sha}" -y`);
      record("last-release.json", { ...receipt, state: "rolled_back" });
    } catch (rollbackError) {
      record("last-release.json", { ...receipt, state: "rollback_failed" });
      throw new AggregateError([error, rollbackError], "Release and automatic edge rollback failed; operator action required");
    }
    throw error;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  release({ full: process.argv.includes("--full") }).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
