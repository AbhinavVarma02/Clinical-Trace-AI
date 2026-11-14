// Hosted Epic Beta release: one owner-approved publication copy on the static, assets-only Beta Worker.
// Publishing the copy needs its own explicit owner approval (the Epic terms are the owner's decision), and the
// copy is passed by path and pinned SHA-256; it is never stored in Git. Deploy or smoke failure rolls back.
import { createHash } from "node:crypto";
import { execSync } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// Reuses the Stable release guards by import only; the Stable release script itself is unchanged.
import { loopbackReferences, pinnedAccount, readTree, verifyAccount } from "./release.mjs";

const here = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(here, "..", "..");
export const BETA_URL = "https://clinical-trace-ai-beta.vathadiabhinav.workers.dev";
/** The Beta source lives on feature/beta; the owner confirms this branch before the first publication. */
export const BETA_RELEASE_BRANCHES = ["feature/beta"];
const CONFIG = "beta/wrangler.jsonc";
const PUBLIC = join(here, "beta", "public");
const COPY_PATH = "beta/app/published/record.json";
const FORMAT = "clinical-trace-epic-beta-publication/1";
const SECTIONS = ["conditions", "encounters", "labs", "vitals", "reports", "notes"];
// Owner and WebAuthn code must never ship in the published build.
const FORBIDDEN = ["/owner/", "navigator.credentials", "genuine-webauthn"];

/** Refuse unless HEAD is exactly the tip of refs/heads/<branch> on origin, so only pushed, reviewable commits ship. */
export function requirePublishedTip(sh, branch) {
  const head = sh("git rev-parse HEAD", repo).trim();
  const ref = `refs/heads/${branch}`;
  const tip = sh(`git ls-remote origin ${ref}`, repo).split("\n").map(line => line.trim().split(/\s+/)).find(([, name]) => name === ref)?.[0];
  if (!/^[0-9a-f]{40}$/.test(head) || tip !== head) {
    throw new Error(`Refusing to release ${head.slice(0, 7) || "an unknown commit"}: it is not the published tip of origin/${branch}`);
  }
}

const shell = (cmd, cwd = here) => execSync(cmd, { cwd, stdio: ["ignore", "pipe", "inherit"], encoding: "utf8" });
const execute = (cmd, cwd = here, env = {}) => execSync(cmd, { cwd, stdio: "inherit", env: { ...process.env, ...env } });
const save = (name, value) => {
  mkdirSync(join(here, ".wrangler"), { recursive: true });
  writeFileSync(join(here, ".wrangler", name), JSON.stringify(value, null, 1) + "\n");
};

/** Accept only the approved bytes of a genuine Epic sandbox copy, labelled and with the attempt identity withheld. */
export function checkCopy(bytes, sha256) {
  if (!/^[a-f0-9]{64}$/.test(sha256 ?? "") || createHash("sha256").update(bytes).digest("hex") !== sha256) {
    throw new Error("The publication copy does not match the approved SHA-256; release stopped");
  }
  const document = JSON.parse(bytes.toString("utf8"));
  const view = document?.view;
  const provenance = view?.provenance;
  const record = view?.record;
  const warnings = Array.isArray(view?.warnings) ? view.warnings.map(w => w?.code) : [];
  // The same checks the hosted page makes, plus every synthetic marker, so a site that deploys also renders.
  if (document?.format !== FORMAT || !Array.isArray(document.withheld) || !document.withheld.includes("attempt_id") ||
      view?.mode !== "LIVE" || view.data_origin !== "epic_sandbox" || view.label !== "Approved LIVE patient" ||
      view.source !== "epic-fhir-sandbox-r4" || view.publication !== "published_snapshot" || view.completeness !== "unconfirmed" ||
      view.serving !== "stale" || view.freshness !== "stale" || view.evidence_error !== null ||
      typeof provenance !== "object" || provenance === null || provenance.synthetic_transport !== false ||
      Object.hasOwn(provenance, "attempt_id") || provenance.mode !== "LIVE" || provenance.source !== view.source ||
      !/^[a-f0-9]{32}$/.test(view.active?.generation_id ?? "") || provenance.generation_id !== view.active.generation_id ||
      !/^[a-f0-9]{64}$/.test(provenance.manifest_sha256 ?? "") || !/^[a-f0-9]{64}$/.test(provenance.bundle_sha256 ?? "") ||
      typeof record?.patient !== "object" || !SECTIONS.every(name => Array.isArray(record[name]?.items)) ||
      !warnings.includes("published_snapshot") || warnings.includes("synthetic_live_fixture") ||
      !record.notes.items.every(item => item?.source?.synthetic === false)) {
    throw new Error("Only an approved Epic sandbox publication copy can be released; release stopped");
  }
  return document;
}

const CSP = [
  "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'", "img-src 'self' data:", "font-src 'self'",
  "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'self'", "form-action 'none'", "object-src 'none'",
].join("; ");

/** Add the copy, headers and redirects to a built tree (the published Beta app is already in beta/app). */
export function assemble(dir, bytes) {
  mkdirSync(join(dir, "beta", "app", "published"), { recursive: true });
  writeFileSync(join(dir, "beta", "app", "published", "record.json"), bytes);
  writeFileSync(join(dir, "_headers"), `/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), publickey-credentials-get=(), publickey-credentials-create=()
  X-Frame-Options: DENY
  Strict-Transport-Security: max-age=31536000
  X-Robots-Tag: noindex, nofollow, noarchive
  Content-Security-Policy: ${CSP}
/beta/app/assets/*
  Cache-Control: public, max-age=31536000, immutable
/beta/app/published/*
  Cache-Control: no-store
/beta/app/
  Cache-Control: no-cache
`);
  writeFileSync(join(dir, "_redirects"), "/ /beta/app/ 302\n/beta /beta/app/ 301\n/beta/app /beta/app/ 301\n");
  writeFileSync(join(dir, ".assetsignore"), "beta/app/.vite\n"); // the build manifest is not part of the site
}

/** Published code that must not ship: loopback addresses or any owner/WebAuthn code. The approved copy is data,
 *  already pinned by hash and checked by checkCopy, so its clinical text cannot trip a code pattern. */
export function forbiddenReferences(files) {
  const code = files.filter(f => f.path !== COPY_PATH);
  return [...new Set([...loopbackReferences(code), ...code.filter(f => FORBIDDEN.some(s => f.text.includes(s))).map(f => f.path)])];
}

/** The deployed site must serve the app, its headers and exactly the approved copy. */
export async function smoke(base, sha256, get = fetch) {
  const page = await get(`${base}/beta/app/`, { redirect: "error" });
  const html = await page.text();
  if (page.status !== 200 || !html.includes('<div id="root">')) throw new Error("Beta smoke: app page failed");
  if (!page.headers.get("content-security-policy")?.includes("default-src 'self'") || !page.headers.get("x-robots-tag")?.includes("noindex")) {
    throw new Error("Beta smoke: security headers missing");
  }
  const copy = await get(`${base}/beta/app/published/record.json`, { redirect: "error" });
  const digest = createHash("sha256").update(Buffer.from(await copy.arrayBuffer())).digest("hex");
  if (copy.status !== 200 || digest !== sha256) throw new Error("Beta smoke: served copy differs from the approved copy");
}

export async function releaseBeta({ copy, sha256, firstDeploy = false, sh = shell, run = execute, record = save,
  account = pinnedAccount(readFileSync(join(here, "beta", "wrangler.jsonc"), "utf8")),
  delay = () => new Promise(r => setTimeout(r, 5000)), probe = smoke, attempts = 6, readCopy = path => readFileSync(path),
  // site is injectable for tests only; wrangler deploys beta/public as configured in beta/wrangler.jsonc.
  site = PUBLIC, published = () => readTree(site), clear = () => {
    mkdirSync(site, { recursive: true });
    for (const entry of readdirSync(site)) rmSync(join(site, entry), { recursive: true, force: true });
  } } = {}) {
  const branch = sh("git branch --show-current", repo).trim();
  if (!BETA_RELEASE_BRANCHES.includes(branch)) {
    throw new Error(`Refusing to release the Beta from branch "${branch || "(detached HEAD)"}"; it is released only from ${BETA_RELEASE_BRANCHES.join(", ")}`);
  }
  const dirty = sh("git status --porcelain -- frontend deploy/cloudflare", repo).trim();
  if (dirty) throw new Error(`Refusing to release uncommitted sources:\n${dirty}`);
  requirePublishedTip(sh, branch);
  const sha = sh("git rev-parse --short HEAD", repo).trim();
  if (!/^[0-9a-f]{7,40}$/.test(sha)) throw new Error("Cannot identify release commit");
  const bytes = readCopy(copy);
  checkCopy(bytes, sha256);
  verifyAccount(sh, account);
  clear();
  // Invented preview fixtures stay refused in the published build, whatever the local environment says.
  run(`npx vite build --config vite.beta.config.ts --outDir "${join(site, "beta", "app")}" --emptyOutDir`, join(repo, "frontend"),
    { VITE_BETA_CLIENT: "published", VITE_BETA_ALLOW_FIXTURE: "false" });
  if (!published().some(file => file.path === "beta/app/index.html")) throw new Error("The published Beta build is missing; release stopped");
  assemble(site, bytes);
  const refused = forbiddenReferences(published());
  if (refused.length) throw new Error(`Refusing to publish loopback addresses or owner code in: ${refused.join(", ")}`);

  let previous = null;
  let versions = null;
  try {
    versions = JSON.parse(sh(`npx wrangler deployments list --config ${CONFIG} --json`));
  } catch (error) {
    if (!firstDeploy) throw error;
  }
  if (firstDeploy) {
    if (versions?.length) throw new Error("The Beta Worker already has deployments; use a normal release with rollback");
  } else {
    versions.sort((a, b) => String(a.created_on).localeCompare(String(b.created_on)));
    const latest = versions.at(-1);
    previous = latest?.versions?.length === 1 ? latest.versions[0]?.version_id : null;
    if (typeof previous !== "string" || !/^[0-9a-f-]{36}$/i.test(previous)) {
      throw new Error("No unambiguous previous Beta version; release stopped before publication");
    }
  }
  const receipt = { sha, account, previous, copy_sha256: sha256, first_deploy: firstDeploy, at: new Date().toISOString(), state: "prepared" };
  record("beta-last-release.json", receipt);
  try {
    verifyAccount(sh, account);
    run(`npx wrangler deploy --config ${CONFIG}`);
    // A new workers.dev name can take a little while to answer; retry the smoke before declaring failure.
    for (let attempt = 1; ; attempt += 1) {
      await delay();
      try {
        await probe(BETA_URL, sha256);
        break;
      } catch (error) {
        if (attempt >= attempts) throw error;
      }
    }
    record("beta-last-release.json", { ...receipt, state: "passed" });
    console.log(`Released the Beta from ${sha} at ${BETA_URL}/beta/app/`);
  } catch (error) {
    if (!previous) {
      record("beta-last-release.json", { ...receipt, state: "first_deploy_failed" });
      throw new AggregateError([error], "First Beta deployment failed with no previous version; operator action required (wrangler delete --config beta/wrangler.jsonc)");
    }
    record("beta-last-release.json", { ...receipt, state: "failed" });
    try {
      run(`npx wrangler rollback ${previous} --config ${CONFIG} --message "automatic rollback: Beta release failed for ${sha}" -y`);
      record("beta-last-release.json", { ...receipt, state: "rolled_back" });
    } catch (rollbackError) {
      record("beta-last-release.json", { ...receipt, state: "rollback_failed" });
      throw new AggregateError([error, rollbackError], "Beta release and automatic rollback failed; operator action required");
    }
    throw error;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const value = name => { const at = process.argv.indexOf(name); return at > 0 ? process.argv[at + 1] : null; };
  releaseBeta({ copy: value("--copy"), sha256: value("--copy-sha256"), firstDeploy: process.argv.includes("--first-deploy") }).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
