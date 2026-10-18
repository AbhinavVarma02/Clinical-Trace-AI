import { describe, expect, it, vi } from "vitest";
import { estimateNeurons, limits, MODEL_PRICES, secondsUntilUtcMidnight, type Env } from "../src/config";
import { classifyBackendResponse, handleApi, matchRoute } from "../src/gateway";
import { clientKey, safeEqual } from "../src/http";
import { adaptMessages, handleChatCompletions, isQuotaError, normaliseOutput, validateChat } from "../src/inference";
import { percentile } from "../src/status";

/** Minimal in-memory stand-in for the D1 statements this Worker issues. */
class FakeD1 {
  counters = new Map<string, number>();
  events: unknown[][] = [];
  state = new Map<string, string>();
  prepare(sql: string) {
    const db = this;
    let args: unknown[] = [];
    const stmt = {
      bind(...values: unknown[]) { args = values; return stmt; },
      async first<T>(): Promise<T | null> {
        if (sql.startsWith("INSERT INTO daily_counter")) {
          const key = `${args[0]}|${args[1]}|${args[2]}`;
          const value = (db.counters.get(key) ?? 0) + Number(args[3]);
          db.counters.set(key, value);
          return { value } as T;
        }
        if (sql.startsWith("SELECT value FROM daily_counter")) {
          const value = db.counters.get(`${args[0]}|${args[1]}|${args[2]}`);
          return (value === undefined ? null : { value }) as T | null;
        }
        if (sql.startsWith("DELETE FROM edge_state")) {
          const value = db.state.get("wake_started");
          db.state.delete("wake_started");
          return (value ? { value } : null) as T | null;
        }
        return null;
      },
      async run() {
        if (sql.startsWith("INSERT INTO edge_event")) db.events.push(args);
        if (sql.startsWith("INSERT OR IGNORE INTO edge_state") && !db.state.has("wake_started")) db.state.set("wake_started", String(args[0]));
        return { success: true };
      },
      async all() { return { results: [] }; },
    };
    return stmt;
  }
  async batch() { return []; }
}

function makeEnv(overrides: Partial<Env> = {}, ai?: (model: string, input: unknown) => unknown): Env & { DB: FakeD1 } {
  return {
    ASSETS: { fetch: async () => new Response("asset") } as unknown as Fetcher,
    AI: { run: vi.fn(async (model: string, input: unknown) => (ai ? ai(model, input) : { response: "{}", usage: { prompt_tokens: 10, completion_tokens: 5, neurons: 0.5 } })) } as unknown as Ai,
    DB: new FakeD1() as unknown as D1Database & FakeD1,
    BACKEND_ORIGIN: "https://backend.example",
    EDGE_BACKEND_SECRET: "edge-secret-value",
    LLM_PROXY_TOKEN: "proxy-token",
    LLM_MODEL: "@cf/qwen/qwen3-30b-a3b-fp8",
    DAILY_NEURON_BUDGET: "8000",
    DAILY_AGENT_RUNS_GLOBAL: "3",
    DAILY_AGENT_RUNS_PER_CLIENT: "2",
    RELEASE: "test",
    ...overrides,
  } as Env & { DB: FakeD1 };
}

const ctx = () => {
  const pending: Promise<unknown>[] = [];
  return { waitUntil: (p: Promise<unknown>) => { pending.push(p); }, passThroughOnException() {}, props: {}, pending } as unknown as ExecutionContext & { pending: Promise<unknown>[] };
};

describe("configuration and accounting", () => {
  it("caps the neuron budget below the free allocation", () => {
    expect(limits(makeEnv({ DAILY_NEURON_BUDGET: "50000" })).neuronBudget).toBe(9500);
    expect(limits(makeEnv({ DAILY_NEURON_BUDGET: "nonsense" })).neuronBudget).toBe(8000);
  });
  it("prices only allow-listed models", () => {
    expect(estimateNeurons("@cf/unknown/model", 10, 10)).toBe(Number.POSITIVE_INFINITY);
    // 1M input tokens of Qwen3 at $0.0509 = 4627 neurons at $0.011 per 1,000.
    expect(Math.round(estimateNeurons("@cf/qwen/qwen3-30b-a3b-fp8", 1_000_000, 0))).toBe(4627);
    expect(Object.keys(MODEL_PRICES)).toContain("@cf/meta/llama-3.3-70b-instruct-fp8-fast");
  });
  it("counts down to the UTC reset", () => {
    expect(secondsUntilUtcMidnight(new Date("2026-10-17T23:59:30Z"))).toBe(30);
  });
  it("computes nearest-rank percentiles", () => {
    expect(percentile([], 50)).toBeNull();
    expect(percentile([5, 1, 3, 2, 4], 50)).toBe(3);
    expect(percentile([5, 1, 3, 2, 4], 95)).toBe(5);
  });
});

describe("client identity", () => {
  it("is salted per day and never contains the address", async () => {
    const request = new Request("https://edge.example/api/v2/patients", { headers: { "cf-connecting-ip": "203.0.113.9" } });
    const a = await clientKey(request, "salt", "2026-10-17");
    const b = await clientKey(request, "salt", "2026-10-18");
    expect(a).toMatch(/^[0-9a-f]{24}$/);
    expect(a).not.toBe(b);
    expect(a).not.toContain("203");
  });
  it("compares secrets in constant time semantics", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
  });
});

describe("gateway", () => {
  it("allow-lists the workspace routes only", () => {
    expect(matchRoute("POST", "/api/v2/agent/runs/stream")?.kind).toBe("agent");
    expect(matchRoute("GET", "/api/v2/agent/runs/abc-123")?.template).toBe("/api/v2/agent/runs/{id}");
    expect(matchRoute("GET", "/api/v2/patients/synthetic_p001/resources/Observation/obs-1/provenance")?.kind).toBe("read");
    expect(matchRoute("DELETE", "/api/v2/reviews/abc")).toBeNull();
    expect(matchRoute("GET", "/api/v1/predict")).toBeNull();
    expect(matchRoute("GET", "/api/v2/patients/../../etc")).toBeNull();
    expect(matchRoute("POST", "/api/v2/agent/runs/x/resume")?.kind).toBe("agent");
  });
  it("classifies sleeping-host responses but passes application errors through", () => {
    expect(classifyBackendResponse(503, "text/html")).toBe("backend_waking");
    expect(classifyBackendResponse(200, "text/html; charset=utf-8")).toBe("backend_waking");
    expect(classifyBackendResponse(404, "text/html")).toBe("backend_unavailable");
    expect(classifyBackendResponse(503, "application/json")).toBeNull();
    expect(classifyBackendResponse(200, "text/event-stream")).toBeNull();
  });

  it("forwards allow-listed headers with the edge secret and a salted client key", async () => {
    const env = makeEnv();
    const fetchMock = vi.fn(async (_url: URL, _init?: RequestInit) => new Response("[]", { headers: { "content-type": "application/json", "set-cookie": "x=1" } }));
    vi.stubGlobal("fetch", fetchMock);
    const response = await handleApi(new Request("https://edge.example/api/v2/patients", {
      headers: { "x-demo-persona": "clinician.demo", "x-demo-workspace": "a".repeat(32), cookie: "secret", authorization: "Bearer leak", "cf-connecting-ip": "203.0.113.9" },
    }), env, ctx(), "rid-1");
    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).toBeNull();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("https://backend.example/api/v2/patients");
    const sent = new Headers(init!.headers);
    expect(sent.get("x-edge-secret")).toBe("edge-secret-value");
    expect(sent.get("x-demo-workspace")).toBe("a".repeat(32));
    expect(sent.get("x-edge-client")).toMatch(/^[0-9a-f]{24}$/);
    expect(sent.get("cookie")).toBeNull();
    expect(sent.get("authorization")).toBeNull();
    expect([...sent.values()].join(" ")).not.toContain("203.0.113.9");
    vi.unstubAllGlobals();
  });

  it("turns a sleeping backend into a typed, retryable error and records the cold start", async () => {
    const env = makeEnv();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>Starting</html>", { status: 503, headers: { "content-type": "text/html" } })));
    const c = ctx();
    const response = await handleApi(new Request("https://edge.example/api/v2/system/status"), env, c, "rid-2");
    await Promise.all(c.pending);
    expect(response.status).toBe(503);
    expect(response.headers.get("retry-after")).toBe("5");
    expect((await response.json() as { error: { code: string } }).error.code).toBe("backend_waking");
    expect(env.DB.state.has("wake_started")).toBe(true);
    vi.unstubAllGlobals();
  });

  it("enforces per-visitor and global daily agent-run caps before contacting the backend", async () => {
    const env = makeEnv();
    const fetchMock = vi.fn(async () => new Response("{}", { headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const run = (ip: string) => handleApi(new Request("https://edge.example/api/v2/agent/runs", {
      method: "POST", body: JSON.stringify({ patient_id: "synthetic_p001", question: "q" }),
      headers: { "content-type": "application/json", "cf-connecting-ip": ip },
    }), env, ctx(), "rid");
    expect((await run("198.51.100.1")).status).toBe(200);
    expect((await run("198.51.100.1")).status).toBe(200);
    const third = await run("198.51.100.1");
    expect(third.status).toBe(429);
    expect((await third.json() as { error: { code: string; scope: string } }).error).toMatchObject({ code: "daily_limit", scope: "visitor" });
    // Global cap (3) was reached by the third attempt above, so another visitor is also refused.
    const other = await run("198.51.100.2");
    expect((await other.json() as { error: { scope: string } }).error.scope).toBe("deployment");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.unstubAllGlobals();
  });

  it("rejects malformed workspaces, oversized bodies and unknown routes", async () => {
    const env = makeEnv();
    vi.stubGlobal("fetch", vi.fn());
    expect((await handleApi(new Request("https://edge.example/api/v2/patients", { headers: { "x-demo-workspace": "bad id" } }), env, ctx(), "r")).status).toBe(400);
    expect((await handleApi(new Request("https://edge.example/api/v2/reviews", { method: "POST", body: "x".repeat(40_000) }), env, ctx(), "r")).status).toBe(413);
    expect((await handleApi(new Request("https://edge.example/api/v2/admin"), env, ctx(), "r")).status).toBe(404);
    vi.unstubAllGlobals();
  });
});

describe("inference proxy", () => {
  const chat = (env: Env, body: unknown, token = "proxy-token") => handleChatCompletions(new Request("https://edge.example/edge/llm/v1/chat/completions", {
    method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify(body),
  }), env, ctx(), "rid");
  const messages = [{ role: "system", content: "Reply with JSON." }, { role: "user", content: "Plan tools." }];

  it("requires the backend token", async () => {
    expect((await chat(makeEnv(), { messages }, "wrong")).status).toBe(401);
    expect((await chat(makeEnv({ LLM_PROXY_TOKEN: "" }), { messages }, "")).status).toBe(401);
  });

  it("validates requests and falls back to the configured model", () => {
    expect(validateChat({ messages: [] }, "@cf/qwen/qwen3-30b-a3b-fp8").ok).toBe(false);
    const ok = validateChat({ model: "@cf/unknown", messages: messages as never, max_tokens: 99_999 }, "@cf/qwen/qwen3-30b-a3b-fp8");
    expect(ok).toMatchObject({ ok: true, model: "@cf/qwen/qwen3-30b-a3b-fp8", maxTokens: 1024 });
  });

  it("returns OpenAI-shaped completions and accounts reported neurons", async () => {
    const env = makeEnv({}, () => ({ choices: [{ message: { content: "<think>x</think>{\"tool_calls\": []}" } }], usage: { prompt_tokens: 40, completion_tokens: 8, neurons: 1.25 } }));
    const response = await chat(env, { messages });
    const body = await response.json() as { choices: { message: { content: string } }[]; usage: { prompt_tokens: number }; edge: { estimated_neurons: number; neurons_reported: boolean } };
    expect(body.choices[0]!.message.content).toBe("{\"tool_calls\": []}");
    expect(body.usage.prompt_tokens).toBe(40);
    expect(body.edge).toMatchObject({ estimated_neurons: 1.25, neurons_reported: true });
    const sent = (env.AI.run as ReturnType<typeof vi.fn>).mock.calls[0]![1] as { messages: { content: string }[] };
    expect(sent.messages.at(-1)!.content.endsWith("/no_think")).toBe(true);
  });

  it("refuses before calling the model when the daily budget would be exceeded", async () => {
    const env = makeEnv();
    const today = new Date().toISOString().slice(0, 10);
    env.DB.counters.set(`${today}|neurons|`, 7999.9);
    const response = await chat(env, { messages, max_tokens: 1000 });
    expect(response.status).toBe(429);
    expect((await response.json() as { error: { code: string } }).error.code).toBe("quota_exhausted");
    expect(env.AI.run).not.toHaveBeenCalled();
  });

  it("maps the platform free-allocation error to quota exhaustion and blocks later calls", async () => {
    const env = makeEnv({}, () => { throw new Error("4006: you have used up your daily free allocation of 10,000 neurons"); });
    const c = ctx();
    const response = await handleChatCompletions(new Request("https://edge.example/x", {
      method: "POST", headers: { authorization: "Bearer proxy-token" }, body: JSON.stringify({ messages }),
    }), env, c, "rid");
    await Promise.all(c.pending);
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toMatch(/^\d+$/);
    const today = new Date().toISOString().slice(0, 10);
    expect(env.DB.counters.get(`${today}|neurons|`)).toBeGreaterThanOrEqual(8000);
  });

  it("normalises the different output shapes", () => {
    expect(normaliseOutput({ response: "hi", usage: { prompt_tokens: 1, completion_tokens: 2 } })).toMatchObject({ text: "hi", promptTokens: 1, completionTokens: 2 });
    expect(normaliseOutput({ response: { a: 1 } }).text).toBe("{\"a\":1}");
    expect(isQuotaError("Error 4006: daily free allocation")).toBe(true);
    expect(isQuotaError("Internal server error")).toBe(false);
    expect(adaptMessages("@cf/meta/llama-3.3-70b-instruct-fp8-fast", messages as never)).toEqual(messages);
  });
});

describe("status resilience", () => {
  it("reports availability even when usage accounting fails", async () => {
    const { handleStatus } = await import("../src/status");
    const env = makeEnv({ BACKEND_ORIGIN: "" });
    (env.DB as unknown as { prepare: () => never }).prepare = () => { throw new Error("D1_ERROR: no such table"); };
    const response = await handleStatus(env, ctx());
    const body = await response.json() as { backend: { state: string }; accounting: string; inference: { quota_state: string } };
    expect(response.status).toBe(200);
    expect(body.backend.state).toBe("not_configured");
    expect(body.accounting).toBe("unavailable");
    expect(body.inference.quota_state).toBe("unknown");
  });
});

describe("stream inference relay", () => {
  it("answers relay frames on Workers AI, posts the completion back and hides them from the browser", async () => {
    const env = makeEnv({}, () => ({ choices: [{ message: { content: "{\"tool_calls\": []}" } }], usage: { prompt_tokens: 30, completion_tokens: 5, neurons: 0.4 } }));
    const relayId = "Rk3xR9pQ2mVb7Lc4Ws8Zt1Yh";
    const wire = [
      'event: progress\ndata: {"run_id":"r1","node":"guard"}\n\n',
      `event: relay\ndata: {"relay_id":"${relayId}","request":{"model":"@cf/qwen/qwen3-30b-a3b-fp8","messages":[{"role":"system","content":"s"},{"role":"user","content":"u"}],"max_tokens":50}}\n\n`,
      'event: result\ndata: {"run_id":"r1","outcome":"answered"}\n\n',
    ].join("");
    const posts: { url: string; body: { status: number; body: { choices: { message: { content: string } }[] } }; headers: Headers }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: URL, init?: RequestInit) => {
      if (String(url).includes("/api/v2/agent/relay/")) {
        posts.push({ url: String(url), body: JSON.parse(String(init!.body)), headers: new Headers(init!.headers) });
        return new Response('{"accepted":true}', { headers: { "content-type": "application/json" } });
      }
      const bytes = new TextEncoder().encode(wire);
      const stream = new ReadableStream<Uint8Array>({ start(c) { for (let i = 0; i < bytes.length; i += 23) c.enqueue(bytes.slice(i, i + 23)); c.close(); } });
      return new Response(stream, { headers: { "content-type": "text/event-stream" } });
    }));
    const c = ctx();
    const response = await handleApi(new Request("https://edge.example/api/v2/agent/runs/stream", {
      method: "POST", body: JSON.stringify({ patient_id: "synthetic_p001", question: "q" }),
      headers: { "content-type": "application/json", "cf-connecting-ip": "198.51.100.7" },
    }), env, c, "rid-relay");
    const text = await response.text();
    await Promise.all(c.pending);
    expect(text).toContain("event: progress");
    expect(text).toContain("event: result");
    expect(text).not.toContain("relay");
    expect(posts).toHaveLength(1);
    expect(posts[0]!.url.endsWith(`/api/v2/agent/relay/${relayId}`)).toBe(true);
    expect(posts[0]!.headers.get("x-edge-secret")).toBe("edge-secret-value");
    expect(posts[0]!.body.status).toBe(200);
    expect(posts[0]!.body.body.choices[0]!.message.content).toBe("{\"tool_calls\": []}");
    expect(env.AI.run).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it("returns quota exhaustion to the backend instead of calling the model", async () => {
    const env = makeEnv();
    env.DB.counters.set(`${new Date().toISOString().slice(0, 10)}|neurons|`, 8000);
    const { runCompletion } = await import("../src/inference");
    const result = await runCompletion(env, ctx(), { messages: [{ role: "user", content: "hi" }] }, "rid", "relay");
    expect(result.status).toBe(429);
    expect((result.body as { error: { code: string } }).error.code).toBe("quota_exhausted");
    expect(env.AI.run).not.toHaveBeenCalled();
  });
});
