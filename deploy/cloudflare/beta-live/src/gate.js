// Hosted LIVE Beta Worker: pure request gate (no Workers runtime imports, so node --test can exercise it).
// Cloudflare Access already blocks unauthenticated visitors at the edge; this gate verifies the Access application
// token again before any backend container is started, and the backend verifies it a third time.

export const API_PREFIX = "/beta/api/";
export const ROUTES = new Map([
  ["GET /beta/api/health", "health"],
  ["GET /beta/api/session", "session"],
  ["GET /beta/api/status", "status"],
  ["GET /beta/api/record", "record"],
  ["POST /beta/api/refresh", "refresh"],
  ["POST /beta/api/approve", "approve"],
  ["POST /beta/api/erase", "erase"],
]);
/** Configuration the backend container needs. Secrets come only from the Worker's secret store. */
export const BACKEND_ENV = [
  "BETA_ACCESS_TEAM_DOMAIN", "BETA_ACCESS_AUD", "BETA_OWNER_EMAIL", "BETA_ORIGIN", "BETA_PATIENT_ALIAS",
  "BETA_PATIENT_ID", "EPIC_CLIENT_ID", "EPIC_KEY_ID", "EPIC_PRIVATE_KEY_PEM", "EPIC_SANDBOX_ENABLED",
];
const OPTIONAL_ENV = ["EPIC_JKU"];
const FORWARDED = ["cf-access-jwt-assertion", "content-type", "origin", "x-beta-client"];
const TEAM = /^https:\/\/[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$/;
const AUD = /^[a-f0-9]{64}$/;
const TOKEN = /^[A-Za-z0-9_-]{2,4096}\.[A-Za-z0-9_-]{2,16384}\.[A-Za-z0-9_-]{2,1024}$/;
const KEYS_FRESH_MS = 10 * 60 * 1000;
const SKEW_S = 60;
export const SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
  "X-Robots-Tag": "noindex, nofollow, noarchive",
};

export class GateError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

export function reject(status, code, extra = {}) {
  return new Response(JSON.stringify({ error: { code } }), {
    status, headers: { "Content-Type": "application/json", ...SECURITY_HEADERS, ...extra },
  });
}

function b64url(text) {
  const base = text.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base + "=".repeat((4 - (base.length % 4)) % 4));
  return Uint8Array.from(binary, c => c.charCodeAt(0));
}

const decoder = new TextDecoder("utf-8", { fatal: true });

/** Every backend variable present and well formed; otherwise nothing reaches the container. */
export function backendEnv(env) {
  const missing = BACKEND_ENV.filter(name => typeof env[name] !== "string" || env[name].trim() === "");
  if (missing.length) throw new GateError(503, "backend_not_configured");
  if (!TEAM.test(env.BETA_ACCESS_TEAM_DOMAIN) || !AUD.test(env.BETA_ACCESS_AUD)) throw new GateError(503, "backend_not_configured");
  const out = {};
  for (const name of [...BACKEND_ENV, ...OPTIONAL_ENV]) if (typeof env[name] === "string" && env[name] !== "") out[name] = env[name];
  return out;
}

/** Access key cache, per isolate. */
export class AccessKeys {
  constructor(team, get = fetch, now = Date.now) {
    this.url = `${team}/cdn-cgi/access/certs`;
    this.get = get;
    this.now = now;
    this.keys = new Map();
    this.fetched = 0;
    this.attempted = 0;
    this.pending = null;
  }

  async key(kid) {
    // Concurrent first requests share one in-flight fetch instead of failing inside the refresh window.
    if (this.pending) await this.pending;
    const now = this.now();
    if (!(now - this.fetched < KEYS_FRESH_MS && this.keys.has(kid)) && now - this.attempted >= 30_000) {
      this.attempted = now;
      this.pending = this.refresh(now).finally(() => { this.pending = null; });
      await this.pending;
    }
    const key = this.keys.get(kid);
    if (!key || this.now() - this.fetched >= 6 * 3600 * 1000) throw new GateError(401, "owner_required");
    return key;
  }

  async refresh(now) {
    try {
      const response = await this.get(this.url, { redirect: "error", headers: { Accept: "application/json" } });
      if (response.ok) {
        const body = await response.json();
        const keys = new Map();
        for (const jwk of Array.isArray(body?.keys) ? body.keys : []) {
          if (jwk?.kty !== "RSA" || (jwk.alg ?? "RS256") !== "RS256" || typeof jwk.kid !== "string") continue;
          const key = await crypto.subtle.importKey("jwk", { kty: "RSA", n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
            { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
          keys.set(jwk.kid, key);
        }
        if (keys.size) { this.keys = keys; this.fetched = now; }
      }
    } catch { /* a recent cached key may still be used */ }
  }
}

/** ASCII-only, ASCII-case-insensitive comparison, identical to access.py: no Unicode case folding of lookalikes. */
export function asciiEmailEquals(claim, owner) {
  if (typeof claim !== "string" || !/^[!-~]{3,320}$/.test(claim) || !/^[!-~]{3,320}$/.test(owner)) return false;
  const lower = value => value.replace(/[A-Z]/g, c => c.toLowerCase());
  return lower(claim) === lower(owner);
}

/** Same rules as api/beta_hosted/access.py: RS256, team issuer, AUD tag, times, app token, the one owner email. */
export async function verifyAccess(request, env, keys, nowSeconds = Date.now() / 1000) {
  const values = request.headers.get("cf-access-jwt-assertion");
  if (!values || values.includes(",") || !TOKEN.test(values)) throw new GateError(401, "owner_required");
  const [head, body, signature] = values.split(".");
  let header, claims;
  try {
    header = JSON.parse(decoder.decode(b64url(head)));
    claims = JSON.parse(decoder.decode(b64url(body)));
  } catch {
    throw new GateError(401, "owner_required");
  }
  if (header?.alg !== "RS256" || "crit" in header || typeof header.kid !== "string") throw new GateError(401, "owner_required");
  const key = await keys.key(header.kid);
  const valid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64url(signature), new TextEncoder().encode(`${head}.${body}`));
  if (!valid) throw new GateError(401, "owner_required");
  const audiences = Array.isArray(claims?.aud) ? claims.aud : [claims?.aud];
  const { exp, iat } = claims;
  const nbf = claims.nbf ?? iat;
  const times = [exp, iat, nbf].every(value => typeof value === "number" && Number.isFinite(value));
  if (!audiences.includes(env.BETA_ACCESS_AUD) || claims.iss !== env.BETA_ACCESS_TEAM_DOMAIN || !times ||
      !(exp > nowSeconds && nbf <= nowSeconds + SKEW_S && iat <= nowSeconds + SKEW_S && iat < exp) ||
      (claims.type ?? "app") !== "app" || "common_name" in claims ||
      !asciiEmailEquals(claims.email, env.BETA_OWNER_EMAIL.trim())) {
    throw new GateError(401, "owner_required");
  }
  return { expires: exp };
}

/** Route, method, query and cross-site checks before any container work. Returns the route name. */
export function admit(request, env) {
  const url = new URL(request.url);
  const name = ROUTES.get(`${request.method} ${url.pathname}`);
  if (!name) throw new GateError([...ROUTES.keys()].some(key => key.endsWith(` ${url.pathname}`)) ? 405 : 404, "not_found");
  if (url.search) throw new GateError(400, "unexpected_payload");
  // Every API route, GET included: a cross-site page cannot add x-beta-client without a CORS preflight (never
  // answered), and a top-level navigation is refused, so another site cannot even wake the billed container.
  if (request.headers.get("x-beta-client") !== "live" || (request.headers.get("sec-fetch-site") ?? "same-origin") !== "same-origin") {
    throw new GateError(403, "same_origin_required");
  }
  if (request.method === "POST") {
    if (request.headers.get("origin") !== env.BETA_ORIGIN) throw new GateError(403, "same_origin_required");
    if (name === "refresh" && env.EPIC_SANDBOX_ENABLED !== "1") throw new GateError(503, "live_disabled");
  }
  return name;
}

/** Only the headers the backend reads are forwarded; everything else (cookies, Cloudflare and proxy headers) is dropped. */
export function forwardHeaders(request) {
  const headers = new Headers();
  for (const name of FORWARDED) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  return headers;
}

export async function configDigest(values) {
  const text = JSON.stringify(Object.keys(values).sort().map(key => [key, values[key]]));
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}

export function secured(response) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value);
  headers.delete("set-cookie");
  return new Response(response.body, { status: response.status, headers });
}
