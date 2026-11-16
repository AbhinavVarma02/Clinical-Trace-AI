// Hosted LIVE Beta Worker: owner-only, behind Cloudflare Access.
//   /beta/api/*  -> Access token check, route/CSRF gate -> the single backend container (Durable Object "owner")
//   everything else -> the static Beta UI assets (served directly, with their _headers)
// The container is reachable only through this Worker's Durable Object binding; it has no public address.
import { DurableObject } from "cloudflare:workers";
import { AccessKeys, GateError, admit, backendEnv, configDigest, forwardHeaders, reject, secured, verifyAccess } from "./gate.js";

const PORT = 8080;
const READY_MS = 90_000;
// Each request resets this, so a pause between Fetch and Approve keeps the staged candidate; then it sleeps.
const IDLE_MS = 10 * 60 * 1000;
let accessKeys = null;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export class BetaBackend extends DurableObject {
  async fetch(request) {
    const container = this.ctx.container;
    let values;
    try {
      values = backendEnv(this.env);
    } catch (error) {
      return reject(error.status ?? 503, error.code ?? "backend_not_configured");
    }
    // Configuration is passed at start; a changed secret or kill switch restarts the container (ephemeral data is lost).
    const digest = await configDigest(values);
    if (container.running && (await this.ctx.storage.get("config")) !== digest) await container.destroy("configuration changed");
    if (!container.running) {
      container.start({ enableInternet: true, env: values });
      await this.ctx.storage.put("config", digest);
      // `running` can lag a cold start; an exit (for example a configuration refusal) ends the wait instead.
      this.exited = false;
      container.monitor().then(() => { this.exited = true; }, () => { this.exited = true; });
    }
    await container.setInactivityTimeout(IDLE_MS);
    const port = container.getTcpPort(PORT);
    const deadline = Date.now() + READY_MS;
    for (;;) {
      try {
        const health = await port.fetch(`http://backend:${PORT}/beta/api/health`);
        if (health.ok) break;
      } catch { /* starting */ }
      if (Date.now() > deadline || this.exited) return reject(503, "backend_starting");
      await sleep(500);
    }
    return port.fetch(request);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/beta/api/")) return env.ASSETS.fetch(request);
    try {
      admit(request, env);
      // Every API route, health included, needs the owner's Access token: nothing anonymous can start the container.
      if (!env.BETA_ACCESS_TEAM_DOMAIN || !env.BETA_ACCESS_AUD || !env.BETA_OWNER_EMAIL) throw new GateError(503, "backend_not_configured");
      accessKeys ??= new AccessKeys(env.BETA_ACCESS_TEAM_DOMAIN);
      await verifyAccess(request, env, accessKeys);
      const { success } = await env.API_LIMITER.limit({ key: "owner" });
      if (!success) throw new GateError(429, "rate_limited");
      const raw = request.method === "POST" ? await request.arrayBuffer() : null;
      if (raw && raw.byteLength > 4096) throw new GateError(413, "payload_too_large");
      const body = raw && raw.byteLength ? raw : undefined; // an empty POST carries no body at all
      const forwarded = new Request(`http://backend:8080${url.pathname}`, { method: request.method, headers: forwardHeaders(request), body });
      const stub = env.BACKEND.get(env.BACKEND.idFromName("owner"));
      return secured(await stub.fetch(forwarded));
    } catch (error) {
      if (error instanceof GateError) {
        // Fixed reason codes only (never a token, claim, email or header value); visible to the owner via `wrangler tail`.
        if (error.status === 401 || error.status === 503) console.log(JSON.stringify({ event: "gate_refused", reason: error.reason }));
        return reject(error.status, error.code, error.status === 429 ? { "Retry-After": "60" } : {});
      }
      return reject(500, "internal_error");
    }
  },
};
