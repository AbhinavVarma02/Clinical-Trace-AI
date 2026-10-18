// Coordinated edge release. Publication still requires explicit owner approval.
// A known previous Worker is required; deploy/smoke failure triggers edge rollback.
// The first deployment to a new account (--first-deploy) has nothing to roll back to.
import { execSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(here, "..", "..");
export const PUBLIC_URL = "https://clinical-trace-ai.vathadiabhinav.workers.dev";
const shell = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: ["ignore", "pipe", "inherit"], encoding: "utf8" });
const execute = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: "inherit" });
const save = (name, value) => {
  mkdirSync(join(here, ".wrangler"), { recursive: true });
  writeFileSync(join(here, ".wrangler", name), JSON.stringify(value, null, 1) + "\n");
};

/** The account pinned in wrangler.jsonc. Every remote step must run against it. */
export function pinnedAccount(config = readFileSync(join(here, "wrangler.jsonc"), "utf8")) {
  const id = config.match(/"account_id"\s*:\s*"([0-9a-f]{32})"/)?.[1];
  if (!id) throw new Error("wrangler.jsonc has no pinned account_id; release stopped");
  return id;
}

/** Refuse to continue unless the active Wrangler login can see the pinned account. */
export function verifyAccount(sh, account) {
  const raw = sh("npx wrangler whoami --json");
  const accounts = JSON.parse(raw.slice(raw.indexOf("{"))).accounts ?? [];
  if (!accounts.some(a => a.id === account)) {
    throw new Error(`Active Wrangler login cannot access pinned account ${account}; release stopped`);
  }
}

export async function release({ full = false, firstDeploy = false, secretsFile = null, sh = shell, run = execute, record = save,
  account = pinnedAccount(), delay = () => new Promise(r => setTimeout(r, 5000)) } = {}) {
  const dirty = sh("git status --porcelain -- frontend deploy/cloudflare", repo).trim();
  if (dirty) throw new Error(`Refusing to release uncommitted sources:\n${dirty}`);
  const sha = sh("git rev-parse --short HEAD", repo).trim();
  if (!/^[0-9a-f]{7,40}$/.test(sha)) throw new Error("Cannot identify release commit");
  verifyAccount(sh, account);
  run("npm run build:workspace", join(repo, "frontend"));
  run("npm run check");
  run("npm run build:assets");

  // Fail before migrations/publication if rollback cannot be prepared. Do not
  // guess a previous version or roll back a deployment split across versions.
  let versions = null;
  try {
    versions = JSON.parse(sh("npx wrangler deployments list --json"));
  } catch (error) {
    if (!firstDeploy) throw error;
  }
  let previous = null;
  if (firstDeploy) {
    if (versions?.length) throw new Error("Worker already has deployments; use a normal release with rollback");
  } else {
    versions.sort((a, b) => String(a.created_on).localeCompare(String(b.created_on)));
    const latest = versions.at(-1);
    previous = latest?.versions?.length === 1 ? latest.versions[0]?.version_id : null;
    if (typeof previous !== "string" || !/^[0-9a-f-]{36}$/i.test(previous)) {
      throw new Error("No unambiguous previous Worker version; release stopped before publication");
    }
  }
  const receipt = { sha, account, previous, first_deploy: firstDeploy, at: new Date().toISOString(), state: "prepared" };
  record("last-release.json", receipt);
  // Database migrations have no automatic rollback. Only compatible additive
  // migrations belong here; review any destructive migration before release.
  try {
    verifyAccount(sh, account);
    run("npx wrangler d1 migrations apply clinical-trace-edge --remote");
  } catch (error) {
    record("last-release.json", { ...receipt, state: "migration_failed" });
    throw error;
  }
  try {
    verifyAccount(sh, account);
    // Secrets upload with the version (values never printed); omitted ones are kept.
    run(`npx wrangler deploy --var RELEASE:${sha}${secretsFile ? ` --secrets-file "${secretsFile}"` : ""}`);
    await delay();
    const out = join(here, ".wrangler", `smoke-${sha}.json`);
    run(`node scripts/smoke.mjs ${PUBLIC_URL} --release ${sha} --out "${out}"${full ? " --full" : ""}`);
    record("last-release.json", { ...receipt, state: "passed" });
    console.log(`Released ${sha}; smoke report ${out}`);
  } catch (error) {
    if (!previous) {
      // No earlier version exists. The new URL is not yet shared, so leave the
      // failed Worker for review instead of deleting anything automatically.
      record("last-release.json", { ...receipt, state: "first_deploy_failed" });
      throw new AggregateError([error], "First deployment failed with no previous version to restore; operator action required");
    }
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
  const at = process.argv.indexOf("--secrets-file");
  release({ full: process.argv.includes("--full"), firstDeploy: process.argv.includes("--first-deploy"),
    secretsFile: at > 0 ? process.argv[at + 1] : null }).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
