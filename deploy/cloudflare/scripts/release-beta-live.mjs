// Hosted LIVE Epic Beta release: the owner-only Worker (clinical-trace-ai-beta-live) with its backend container.
// Separate from Stable and from the invented-data demo Worker (clinical-trace-ai-beta), which stays as the rollback.
// Released only from the pushed tip of feature/beta. The backend image is built from a staged context holding an
// explicit allowlist of tracked files, so untracked or private files can never enter it. No secret is read or written
// here: the owner provisions secrets into the Worker's secret store separately (scripts/provision_beta_live_secrets.py).
// Epic retrieval stays switched off (EPIC_SANDBOX_ENABLED=0) unless --enable-epic is passed for an approved test.
import { execSync } from "node:child_process";
import { copyFileSync, lstatSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { pinnedAccount, readTree, verifyAccount } from "./release.mjs";
import { assemble, forbiddenReferences, requirePublishedTip } from "./release-beta.mjs";

const here = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(here, "..", "..");
export const LIVE_URL = "https://clinical-trace-ai-beta-live.vathadiabhinav.workers.dev";
export const LIVE_RELEASE_BRANCHES = ["feature/beta"];
const CONFIG = "beta-live/wrangler.jsonc";
const SITE = join(here, "beta-live", "public");
const CONTEXT = join(here, "beta-live", ".build", "context");
/** Tracked paths the backend image may contain. Everything else, including tests and docs, stays out. */
export const BACKEND_PATHS = [
  "api/__init__.py", "api/beta_hosted", "api/beta_live_record.py", "api/beta_record.py", "api/beta_oidc_keys.py", "src",
];
const REQUIREMENTS = ["requirements-beta-hosted.txt", "requirements-constraints.txt"];
const PRIVATE = /(^|\/)(\.env[^/]*|.*\.(pem|key|p8|p12|pfx|sqlite|db|zip)|\.dev\.vars.*|\.prod-secrets.*)$/i;
// The live build must have no fixture data, publication loader, owner or WebAuthn code.
const LIVE_FORBIDDEN = ["MOCK Alpha (synthetic)", "published/record.json", "clinical-trace-epic-beta-publication/1"];

const shell = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: ["ignore", "pipe", "inherit"], encoding: "utf8" });
const execute = (cmd, cwd = here, env = {}) => execSync(cmd, { cwd, stdio: "inherit", env: { ...process.env, ...env } });

/** The backend files to stage: tracked, allowlisted, Python only (plus the two requirement files). */
export function backendFiles(sh = shell) {
  // -s gives each entry's mode: only regular files (100644/100755) are staged, never a symlink (120000) or submodule.
  const entries = sh(`git ls-files -s -z -- ${[...BACKEND_PATHS, ...REQUIREMENTS].join(" ")}`, repo).split("\0").filter(Boolean)
    .map(line => { const [meta, path] = line.split("\t"); return { mode: meta.split(" ")[0], path }; });
  const odd = entries.filter(entry => !["100644", "100755"].includes(entry.mode)).map(entry => entry.path);
  if (odd.length) throw new Error(`Refusing to stage links or non-regular entries: ${odd.join(", ")}`);
  const files = entries.map(entry => entry.path).filter(path => REQUIREMENTS.includes(path) || path.endsWith(".py"));
  const refused = files.filter(path => PRIVATE.test(path) || path.includes("..") || path.startsWith("/"));
  if (refused.length) throw new Error(`Refusing to stage private-looking files: ${refused.join(", ")}`);
  for (const required of ["api/beta_hosted/hosted.py", "api/beta_hosted/access.py", "api/beta_hosted/hosted_keys.py", ...REQUIREMENTS]) {
    if (!files.includes(required)) throw new Error(`The backend context is missing ${required}; commit it before releasing`);
  }
  return files;
}

/** Copy the allowlist into a fresh context: requirement files at the root, code under app/. */
export function stageBackend(files, { source = repo, context = CONTEXT, read = path => readFileSync(path, "latin1") } = {}) {
  rmSync(context, { recursive: true, force: true });
  for (const path of files) {
    if (lstatSync(join(source, path)).isSymbolicLink()) throw new Error(`Refusing to stage a link: ${path}`);
    const text = read(join(source, path));
    if (/-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(text)) throw new Error(`Refusing to stage key material from ${path}`);
    const target = REQUIREMENTS.includes(path) ? join(context, path) : join(context, "app", path);
    mkdirSync(dirname(target), { recursive: true });
    copyFileSync(join(source, path), target);
  }
  return context;
}

/** The live UI build: the live client only, no fixtures, loader, owner code or loopback addresses. */
export function checkLiveBuild(files) {
  const refused = forbiddenReferences(files);
  if (refused.length) throw new Error(`Refusing to publish loopback addresses or owner code in: ${refused.join(", ")}`);
  const code = files.filter(f => /\.(js|html)$/.test(f.path)).map(f => f.text).join("\n");
  const found = LIVE_FORBIDDEN.filter(marker => code.includes(marker));
  if (found.length) throw new Error(`The live build contains fixture or publication code (${found.join(", ")}); release stopped`);
  if (!code.includes("/beta/api/") || !code.includes("Epic Beta, hosted live")) throw new Error("The live build lacks the live client; release stopped");
}

/** Access must guard the whole hostname; unauthenticated requests are redirected to the team's sign-in. */
export async function smokeLive(base, { requireAccess = true, get = fetch } = {}) {
  const page = await get(`${base}/beta/app/`, { redirect: "manual" });
  const location = page.headers.get("location") ?? "";
  const guarded = page.status >= 300 && page.status < 400 && /^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com\//.test(location);
  if (guarded) return "access";
  if (requireAccess) throw new Error("Live Beta smoke: Cloudflare Access does not guard the site");
  const html = await page.text();
  if (page.status !== 200 || !html.includes('<div id="root">')) throw new Error("Live Beta smoke: app page failed");
  // Without Access the API must still refuse: no token means no container and no Epic call.
  const api = await get(`${base}/beta/api/session`, { redirect: "manual" });
  if (![401, 503].includes(api.status)) throw new Error(`Live Beta smoke: unauthenticated API answered ${api.status}`);
  return "unguarded";
}

export async function releaseBetaLive({ firstDeploy = false, enableEpic = false, sh = shell, run = execute,
  account = pinnedAccount(readFileSync(join(here, CONFIG), "utf8")), delay = () => new Promise(r => setTimeout(r, 5000)),
  attempts = 8, probe = smokeLive, site = SITE } = {}) {
  const branch = sh("git branch --show-current", repo).trim();
  if (!LIVE_RELEASE_BRANCHES.includes(branch)) throw new Error(`Refusing to release the live Beta from branch "${branch || "(detached HEAD)"}"`);
  const dirty = sh("git status --porcelain -- api src frontend deploy/cloudflare requirements-beta-hosted.txt requirements-constraints.txt", repo).trim();
  if (dirty) throw new Error(`Refusing to release uncommitted sources:\n${dirty}`);
  requirePublishedTip(sh, branch);
  const sha = sh("git rev-parse --short HEAD", repo).trim();
  verifyAccount(sh, account);

  stageBackend(backendFiles(sh));
  rmSync(site, { recursive: true, force: true });
  run(`npx vite build --config vite.beta.config.ts --outDir "${join(site, "beta", "app")}" --emptyOutDir`, join(repo, "frontend"),
    { VITE_BETA_CLIENT: "live", VITE_BETA_ALLOW_FIXTURE: "false" });
  assemble(site, null);
  checkLiveBuild(readTree(site));

  let previous = null;
  if (!firstDeploy) {
    const versions = JSON.parse(sh(`npx wrangler deployments list --config ${CONFIG} --json`));
    versions.sort((a, b) => String(a.created_on).localeCompare(String(b.created_on)));
    const latest = versions.at(-1);
    previous = latest?.versions?.length === 1 ? latest.versions[0]?.version_id : null;
    if (typeof previous !== "string" || !/^[0-9a-f-]{36}$/i.test(previous)) throw new Error("No unambiguous previous live Beta version; release stopped");
  }
  verifyAccount(sh, account);
  run(`npx wrangler deploy --config ${CONFIG} --containers-rollout immediate --var EPIC_SANDBOX_ENABLED:${enableEpic ? "1" : "0"}`);
  try {
    let mode;
    for (let attempt = 1; ; attempt += 1) {
      await delay();
      try {
        mode = await probe(LIVE_URL, { requireAccess: !firstDeploy });
        break;
      } catch (error) {
        if (attempt >= attempts) throw error;
      }
    }
    console.log(`Released the live Beta from ${sha} at ${LIVE_URL}/beta/app/ (Access: ${mode}; Epic retrieval ${enableEpic ? "ENABLED" : "off"})`);
    return { sha, previous, mode, enableEpic };
  } catch (error) {
    if (!previous) throw new AggregateError([error], "First live Beta deployment failed its smoke; delete it with: npx wrangler delete --config beta-live/wrangler.jsonc");
    run(`npx wrangler rollback ${previous} --config ${CONFIG} --message "automatic rollback: live Beta smoke failed for ${sha}" -y`);
    throw error;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  releaseBetaLive({ firstDeploy: process.argv.includes("--first-deploy"), enableEpic: process.argv.includes("--enable-epic") })
    .catch(error => { console.error(error.message); process.exitCode = 1; });
}
