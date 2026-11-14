// Coordinated edge release. Publication still requires explicit owner approval.
// A known previous Worker is required; deploy/smoke failure triggers edge rollback.
// The first deployment to a new account (--first-deploy) has nothing to roll back to.
import { execSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(here, "..", "..");
export const PUBLIC_URL = "https://clinical-trace-ai.vathadiabhinav.workers.dev";
/** The hosted read-only Epic Beta that the Stable Beta link may point to, and nowhere else. */
export const BETA_PUBLIC_URL = "https://clinical-trace-ai-beta.vathadiabhinav.workers.dev/beta/app/";
/** Branches the public edge may be released from; the released commit must be that branch's published tip. */
export const RELEASE_BRANCHES = ["main"];
// `vite build` (production mode) also reads these git-ignored files from the workspace root (frontend/workspace).
const VITE_ENV_FILES = [".env", ".env.local", ".env.production", ".env.production.local"];
const workspaceRoot = join(repo, "frontend", "workspace");
const readOptional = path => (existsSync(path) ? readFileSync(path, "utf8") : null);
const LOOPBACK = new RegExp([
  String.raw`(?:[a-z0-9-]+\.)*localhost\b`,
  String.raw`\b127(?:\.\d{1,3}){3}\b`,
  String.raw`\b0\.0\.0\.0\b`,
  String.raw`\[(?:0{0,4}:)+0{0,4}1\]`, // [::1], [0:0:0:0:0:0:0:1]
  String.raw`\[::\]`,
  String.raw`\[::ffff:(?:7f[0-9a-f]{2}:[0-9a-f]{1,4}|127(?:\.\d{1,3}){3})\]`,
].join("|"), "i");

/** Where VITE_ENABLE_BETA_LINK is set outside this release (environment or git-ignored env files), if anywhere. */
export function betaLinkSources(env = process.env, read = name => readOptional(join(workspaceRoot, name))) {
  const sources = env.VITE_ENABLE_BETA_LINK === undefined ? [] : ["the environment"];
  for (const name of VITE_ENV_FILES) {
    // KEY=value; KEY: value is also flagged, although Vite's parser ignores it.
    if (/^\s*(?:export\s+)?VITE_ENABLE_BETA_LINK(?:\s*=|:\s)/m.test(read(name) ?? "")) sources.push(`frontend/workspace/${name}`);
  }
  return sources;
}

/** Published files that name a loopback or *.localhost address, which no public visitor can reach. */
export function loopbackReferences(files) {
  return files.filter(file => LOOPBACK.test(file.text)).map(file => file.path);
}

/** Every file under dir, as text, for scanning what is about to be published. Links and special files are refused. */
export function readTree(dir) {
  const entries = readdirSync(dir, { recursive: true, withFileTypes: true });
  const unusual = entries.filter(entry => !entry.isFile() && !entry.isDirectory());
  if (unusual.length) throw new Error(`Refusing to publish links or special files: ${unusual.map(entry => entry.name).join(", ")}`);
  return entries.filter(entry => entry.isFile()).map(entry => {
    const path = join(entry.parentPath, entry.name);
    return { path: relative(dir, path).replaceAll("\\", "/"), text: readFileSync(path, "latin1") };
  });
}
const shell = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: ["ignore", "pipe", "inherit"], encoding: "utf8" });
const execute = (cmd, cwd = here, env = {}) => execSync(cmd, { cwd, stdio: "inherit", env: { ...process.env, ...env } });
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

export async function release({ full = false, firstDeploy = false, secretsFile = null, withBetaLink = false, sh = shell, run = execute,
  record = save, account = pinnedAccount(), delay = () => new Promise(r => setTimeout(r, 5000)), betaLink = () => betaLinkSources(),
  published = () => readTree(join(here, "public")) } = {}) {
  // The public edge is released only from the published tip of an approved branch. The Beta link ships only when this
  // release is asked for it (--with-beta-link), and then only to the hosted Beta; a flag set anywhere else is refused.
  const branch = sh("git branch --show-current", repo).trim();
  if (!RELEASE_BRANCHES.includes(branch)) {
    throw new Error(`Refusing to release from branch "${branch || "(detached HEAD)"}"; the public edge is released only from ${RELEASE_BRANCHES.join(", ")}`);
  }
  const flagged = betaLink();
  if (flagged.length) throw new Error(`Refusing to release with VITE_ENABLE_BETA_LINK set in ${flagged.join(", ")}; use --with-beta-link instead`);
  const dirty = sh("git status --porcelain -- frontend deploy/cloudflare", repo).trim();
  if (dirty) throw new Error(`Refusing to release uncommitted sources:\n${dirty}`);
  const head = sh("git rev-parse HEAD", repo).trim();
  const ref = `refs/heads/${branch}`;
  const tip = sh(`git ls-remote origin ${ref}`, repo).split("\n").map(line => line.trim().split(/\s+/)).find(([, name]) => name === ref)?.[0];
  if (!/^[0-9a-f]{40}$/.test(head) || tip !== head) {
    throw new Error(`Refusing to release ${head.slice(0, 7) || "an unknown commit"}: it is not the published tip of origin/${branch}`);
  }
  const sha = sh("git rev-parse --short HEAD", repo).trim();
  if (!/^[0-9a-f]{7,40}$/.test(sha)) throw new Error("Cannot identify release commit");
  verifyAccount(sh, account);
  run("npm run build:workspace", join(repo, "frontend"), { VITE_ENABLE_BETA_LINK: withBetaLink ? "true" : "false" });
  run("npm run check");
  run("npm run build:assets");
  // The built files are the final word: they catch the link however it was enabled, before any remote step.
  const built = published();
  const loopback = loopbackReferences(built);
  if (loopback.length) throw new Error(`Refusing to publish loopback-only addresses (such as the local Beta link) in: ${loopback.join(", ")}`);
  if (built.some(file => file.text.includes(BETA_PUBLIC_URL)) !== withBetaLink) {
    throw new Error(withBetaLink ? "The built workspace lacks the hosted Beta link; release stopped" : "The hosted Beta link is in a release that did not ask for it; release stopped");
  }

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
  release({ full: process.argv.includes("--full"), firstDeploy: process.argv.includes("--first-deploy"), withBetaLink: process.argv.includes("--with-beta-link"),
    secretsFile: at > 0 ? process.argv[at + 1] : null }).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
