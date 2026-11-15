// Offline tests for the hosted LIVE Beta Worker gate and its release script. No network, no deploy, invented keys only.
import assert from "node:assert/strict";
import { generateKeyPairSync, createSign } from "node:crypto";
import { mkdtempSync, readdirSync, readFileSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { AccessKeys, GateError, admit, asciiEmailEquals, backendEnv, configDigest, forwardHeaders, secured, verifyAccess } from "../beta-live/src/gate.js";
import { backendFiles, checkLiveBuild, smokeLive, stageBackend } from "../scripts/release-beta-live.mjs";

const TEAM = "https://invented-team.cloudflareaccess.com";
const AUD = "a".repeat(64);
const OWNER = "owner@invented.example";
const ORIGIN = "https://clinical-trace-ai-beta-live.invented.workers.dev";
const NOW = 1_799_000_000;
const ENV = { BETA_ACCESS_TEAM_DOMAIN: TEAM, BETA_ACCESS_AUD: AUD, BETA_OWNER_EMAIL: OWNER, BETA_ORIGIN: ORIGIN, EPIC_SANDBOX_ENABLED: "1" };
const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const JWK = { ...publicKey.export({ format: "jwk" }), kid: "invented-kid", alg: "RS256", use: "sig" };
const b64 = value => Buffer.from(value).toString("base64url");

function mint(claims = {}, header = {}, key = privateKey) {
  const body = { aud: [AUD], iss: TEAM, email: OWNER, type: "app", iat: NOW - 5, exp: NOW + 600, ...claims };
  for (const [name, value] of Object.entries(body)) if (value === undefined) delete body[name];
  const signing = `${b64(JSON.stringify({ alg: "RS256", kid: "invented-kid", ...header }))}.${b64(JSON.stringify(body))}`;
  const signature = createSign("RSA-SHA256").update(signing).sign(key).toString("base64url");
  return `${signing}.${signature}`;
}

const certs = () => {
  let calls = 0;
  const get = async () => { calls += 1; return new Response(JSON.stringify({ keys: [JWK] }), { status: 200 }); };
  return { get, calls: () => calls };
};
const request = (method, path, headers = {}) => new Request(`${ORIGIN}${path}`, { method, headers });

test("a valid owner Access token passes, and keys are cached", async () => {
  const source = certs();
  const keys = new AccessKeys(TEAM, source.get, () => NOW * 1000);
  for (let i = 0; i < 3; i += 1) {
    const result = await verifyAccess(request("GET", "/beta/api/session", { "cf-access-jwt-assertion": mint() }), ENV, keys, NOW);
    assert.equal(result.expires, NOW + 600);
  }
  assert.equal(source.calls(), 1);
});

test("every malformed, foreign or expired token is refused before the backend", async () => {
  const { privateKey: other } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const cases = {
    missing: null,
    garbage: "a.b.c",
    audience: mint({ aud: ["b".repeat(64)] }),
    issuer: mint({ iss: "https://other.cloudflareaccess.com" }),
    expired: mint({ exp: NOW - 1 }),
    future: mint({ iat: NOW + 600, nbf: NOW + 600 }),
    owner: mint({ email: "someone@invented.example" }),
    service: mint({ email: undefined, common_name: "svc" }),
    alg: mint({}, { alg: "HS256" }),
    kid: mint({}, { kid: "other" }),
    signature: mint({}, {}, other),
  };
  for (const [name, token] of Object.entries(cases)) {
    const keys = new AccessKeys(TEAM, certs().get, () => NOW * 1000);
    const headers = token ? { "cf-access-jwt-assertion": token } : {};
    await assert.rejects(verifyAccess(request("GET", "/beta/api/record", headers), ENV, keys, NOW),
      error => error instanceof GateError && error.status === 401, name);
  }
});

test("concurrent first requests share one key fetch instead of failing", async () => {
  let release, calls = 0;
  const gate = new Promise(resolve => { release = resolve; });
  const get = async () => { calls += 1; await gate; return new Response(JSON.stringify({ keys: [JWK] }), { status: 200 }); };
  const keys = new AccessKeys(TEAM, get, () => NOW * 1000);
  const results = Promise.all([1, 2, 3].map(() => verifyAccess(request("GET", "/beta/api/session", { "cf-access-jwt-assertion": mint() }), ENV, keys, NOW)));
  release();
  assert.equal((await results).length, 3);
  assert.equal(calls, 1);
});

test("owner email comparison is ASCII-only and identical to the backend", () => {
  assert.ok(asciiEmailEquals("OWNER@invented.example", OWNER));
  assert.ok(!asciiEmailEquals("owner@invented.eKample", OWNER));
  assert.ok(!asciiEmailEquals("owner@invented.example ", OWNER));
  assert.ok(!asciiEmailEquals(undefined, OWNER));
});

test("routes, queries, cross-site requests and the kill switch are gated", () => {
  const read = { "x-beta-client": "live" };
  assert.equal(admit(request("GET", "/beta/api/record", read), ENV), "record");
  assert.throws(() => admit(request("GET", "/beta/api/record"), ENV), error => error.status === 403);
  assert.throws(() => admit(request("GET", "/beta/api/status", { ...read, "sec-fetch-site": "cross-site" }), ENV), error => error.status === 403);
  assert.throws(() => admit(request("GET", "/beta/api/other", read), ENV), error => error.status === 404);
  assert.throws(() => admit(request("GET", "/beta/api/refresh", read), ENV), error => error.status === 405);
  assert.throws(() => admit(request("GET", "/beta/api/record?x=1", read), ENV), error => error.status === 400);
  const good = { origin: ORIGIN, "x-beta-client": "live" };
  assert.equal(admit(request("POST", "/beta/api/refresh", good), ENV), "refresh");
  assert.throws(() => admit(request("POST", "/beta/api/refresh", { ...good, origin: "https://evil.example" }), ENV), error => error.status === 403);
  assert.throws(() => admit(request("POST", "/beta/api/approve", { origin: ORIGIN }), ENV), error => error.status === 403);
  assert.throws(() => admit(request("POST", "/beta/api/refresh", good), { ...ENV, EPIC_SANDBOX_ENABLED: "0" }),
    error => error.status === 503 && error.code === "live_disabled");
  assert.equal(admit(request("POST", "/beta/api/erase", good), { ...ENV, EPIC_SANDBOX_ENABLED: "0" }), "erase");
});

test("only the backend's own headers are forwarded and responses are hardened", () => {
  const headers = forwardHeaders(request("POST", "/beta/api/refresh", {
    "cf-access-jwt-assertion": "t", origin: ORIGIN, "x-beta-client": "live", cookie: "CF_Authorization=x",
    "cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9", authorization: "Bearer x",
  }));
  assert.deepEqual([...headers.keys()].sort(), ["cf-access-jwt-assertion", "origin", "x-beta-client"]);
  const response = secured(new Response("{}", { headers: { "set-cookie": "a=b", "cache-control": "public" } }));
  assert.equal(response.headers.get("set-cookie"), null);
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("the backend is never started without complete configuration, and config changes are detected", async () => {
  assert.throws(() => backendEnv({ ...ENV }), error => error.code === "backend_not_configured");
  const full = { ...ENV, BETA_PATIENT_ALIAS: "invented-alpha", BETA_PATIENT_ID: "invented", EPIC_CLIENT_ID: "invented-client",
    EPIC_KEY_ID: "k".repeat(43), EPIC_PRIVATE_KEY_PEM: "pem", UNRELATED: "x" };
  const values = backendEnv(full);
  assert.equal(values.UNRELATED, undefined);
  assert.notEqual(await configDigest(values), await configDigest({ ...values, EPIC_SANDBOX_ENABLED: "0" }));
});

test("the backend context stages only tracked, allowlisted Python and requirement files", () => {
  const files = backendFiles();
  assert.ok(files.includes("api/beta_hosted/hosted.py") && files.includes("src/interop/epic/beta_live_acquisition.py"));
  assert.ok(files.every(path => path.endsWith(".py") || path.startsWith("requirements-")));
  assert.ok(!files.some(path => path.startsWith("tests/") || path.startsWith("docs/") || path.includes("beta_owner") || path.includes("webauthn")));
  const source = mkdtempSync(join(tmpdir(), "beta-live-src-"));
  const context = mkdtempSync(join(tmpdir(), "beta-live-ctx-"));
  try {
    mkdirSync(join(source, "api"), { recursive: true });
    writeFileSync(join(source, "api", "a.py"), "x = 1\n");
    writeFileSync(join(source, "requirements-beta-hosted.txt"), "fastapi\n");
    stageBackend(["api/a.py", "requirements-beta-hosted.txt"], { source, context });
    assert.deepEqual(readdirSync(context).sort(), ["app", "requirements-beta-hosted.txt"]);
    writeFileSync(join(source, "api", "a.py"), "-----BEGIN " + "PRIVATE KEY-----\n"); // split: no literal header in Git
    assert.throws(() => stageBackend(["api/a.py"], { source, context }), /key material/);
  } finally {
    rmSync(source, { recursive: true, force: true });
    rmSync(context, { recursive: true, force: true });
  }
});

test("the live build must carry the live client and nothing else", () => {
  const ok = [{ path: "beta/app/index.html", text: '<div id="root"></div>' }, { path: "beta/app/a.js", text: 'fetch("/beta/api/"+x); "Epic Beta, hosted live."' }];
  checkLiveBuild(ok);
  assert.throws(() => checkLiveBuild([...ok, { path: "beta/app/b.js", text: "MOCK Alpha (synthetic)" }]), /fixture/);
  assert.throws(() => checkLiveBuild([...ok, { path: "beta/app/c.js", text: "navigator.credentials.get()" }]), /owner code/);
  assert.throws(() => checkLiveBuild([ok[0]]), /lacks the live client/);
});

test("the smoke requires Cloudflare Access unless this is the first deploy", async () => {
  const guarded = async () => new Response(null, { status: 302, headers: { location: "https://invented-team.cloudflareaccess.com/cdn-cgi/access/login/x" } });
  assert.equal(await smokeLive(ORIGIN, { get: guarded }), "access");
  const open = async url => url.endsWith("/beta/app/") ? new Response('<div id="root"></div>', { status: 200 }) : new Response("{}", { status: 503 });
  await assert.rejects(smokeLive(ORIGIN, { get: open }), /does not guard/);
  assert.equal(await smokeLive(ORIGIN, { get: open, requireAccess: false }), "unguarded");
  const leaky = async url => new Response(url.endsWith("/beta/app/") ? '<div id="root"></div>' : "{}", { status: 200 });
  await assert.rejects(smokeLive(ORIGIN, { get: leaky, requireAccess: false }), /unauthenticated API/);
});

test("the Worker configuration pins the account, keeps Epic off and carries no secrets", () => {
  const config = readFileSync(new URL("../beta-live/wrangler.jsonc", import.meta.url), "utf8");
  assert.match(config, /"account_id": "00f876c0ebbf368d25feabe3e49cbc0b"/);
  assert.match(config, /"EPIC_SANDBOX_ENABLED": "0"/);
  assert.match(config, /"max_instances": 1/);
  assert.match(config, /"instance_type": "lite"/);
  assert.doesNotMatch(config, /PRIVATE KEY|cloudflareaccess\.com"|@[a-z0-9-]+\.[a-z]/i);
});
