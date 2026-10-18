// OpenAI-compatible chat-completions proxy backed by the Workers AI binding.
//
// Only the backend (holding LLM_PROXY_TOKEN) may call it. Every call is checked against a daily
// neuron budget kept below the Workers Free allocation, so exhaustion degrades to a labelled
// deterministic answer in the backend instead of incurring cost. Prompts are never logged.

import { estimateNeurons, limits, MODEL_PRICES, secondsUntilUtcMidnight, utcDay, type Env } from "./config";
import { errorResponse, json, safeEqual } from "./http";
import { addCounter, getCounter, recordEvent } from "./store";

const MAX_MESSAGES = 8;
const MAX_PROMPT_CHARS = 48_000;
const MAX_COMPLETION_TOKENS = 1024;

interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

interface ChatRequest {
  model?: string;
  messages?: ChatMessage[];
  max_tokens?: number;
  temperature?: number;
  response_format?: { type?: string };
}

export function isQuotaError(message: string): boolean {
  return /\b4006\b|daily free allocation|neurons? (limit|quota)|upgrade to .*paid/i.test(message);
}

/** Remove reasoning blocks some models emit before their answer. */
export function stripReasoning(text: string): string {
  return text.replace(/<think>[\s\S]*?<\/think>/gi, "").trim();
}

/** Normalise the different Workers AI text-generation output shapes. */
export function normaliseOutput(output: unknown): { text: string; promptTokens?: number; completionTokens?: number; neurons?: number } {
  const o = (output ?? {}) as Record<string, any>;
  const usage = (o.usage ?? {}) as Record<string, number | undefined>;
  let text = "";
  if (Array.isArray(o.choices) && o.choices.length) {
    text = String(o.choices[0]?.message?.content ?? o.choices[0]?.text ?? "");
  } else if (typeof o.response === "string") {
    text = o.response;
  } else if (o.response && typeof o.response === "object") {
    text = JSON.stringify(o.response);
  } else if (typeof output === "string") {
    text = output;
  }
  return { text: stripReasoning(text), promptTokens: usage.prompt_tokens, completionTokens: usage.completion_tokens,
    neurons: typeof usage.neurons === "number" ? usage.neurons : undefined };
}

/**
 * Model-specific prompt adapters. Qwen3 reasons before answering unless told not to; the agent
 * needs short JSON, so the documented `/no_think` switch is appended to the final user turn.
 */
export function adaptMessages(model: string, messages: ChatMessage[]): ChatMessage[] {
  if (!model.startsWith("@cf/qwen/qwen3")) return messages;
  const out = messages.map((m) => ({ ...m }));
  for (let i = out.length - 1; i >= 0; i--) {
    if (out[i]!.role === "user") {
      out[i]!.content = `${out[i]!.content}
/no_think`;
      break;
    }
  }
  return out;
}

export function validateChat(body: ChatRequest, defaultModel: string): { ok: true; model: string; messages: ChatMessage[]; maxTokens: number } | { ok: false; message: string } {
  const model = body.model && MODEL_PRICES[body.model] ? body.model : defaultModel;
  if (!MODEL_PRICES[model]) return { ok: false, message: "model is not on the edge allow-list" };
  const messages = body.messages;
  if (!Array.isArray(messages) || messages.length === 0 || messages.length > MAX_MESSAGES) {
    return { ok: false, message: `messages must contain 1-${MAX_MESSAGES} entries` };
  }
  let chars = 0;
  for (const m of messages) {
    if (!m || !["system", "user", "assistant"].includes(m.role) || typeof m.content !== "string") {
      return { ok: false, message: "each message needs a role and string content" };
    }
    chars += m.content.length;
  }
  if (chars > MAX_PROMPT_CHARS) return { ok: false, message: "prompt exceeds the edge size limit" };
  const maxTokens = Math.min(Math.max(1, Math.floor(body.max_tokens ?? 700)), MAX_COMPLETION_TOKENS);
  return { ok: true, model, messages, maxTokens };
}

export async function handleChatCompletions(request: Request, env: Env, ctx: ExecutionContext, rid: string): Promise<Response> {
  const expected = env.LLM_PROXY_TOKEN ?? "";
  const auth = request.headers.get("authorization") ?? "";
  if (!expected || !safeEqual(auth, `Bearer ${expected}`)) {
    return errorResponse(401, "unauthorized", "inference proxy requires the backend token", rid);
  }
  let body: ChatRequest;
  try {
    body = (await request.json()) as ChatRequest;
  } catch {
    return errorResponse(400, "invalid_json", "request body must be JSON", rid);
  }
  const checked = validateChat(body, env.LLM_MODEL);
  if (!checked.ok) return errorResponse(400, "invalid_request", checked.message, rid);

  const day = utcDay();
  const budget = limits(env).neuronBudget;
  const promptChars = checked.messages.reduce((n, m) => n + m.content.length, 0);
  const worstCase = estimateNeurons(checked.model, Math.ceil(promptChars / 3), checked.maxTokens);
  const used = await getCounter(env.DB, day, "neurons");
  const retryAfter = secondsUntilUtcMidnight();
  if (used + worstCase > budget) {
    ctx.waitUntil(recordEvent(env.DB, day, { kind: "llm", route: "/edge/llm/v1/chat/completions", status: 429, outcome: "budget_exhausted", latencyMs: 0 }));
    return errorResponse(429, "quota_exhausted", "The daily free inference budget is used up; it resets at 00:00 UTC.", rid,
      { retry_after_s: retryAfter }, { "retry-after": String(retryAfter) });
  }

  const started = Date.now();
  const input: Record<string, unknown> = {
    messages: adaptMessages(checked.model, checked.messages),
    max_tokens: checked.maxTokens,
    temperature: Math.min(Math.max(body.temperature ?? 0, 0), 1),
  };
  if (body.response_format?.type === "json_object" && MODEL_PRICES[checked.model]!.jsonMode) {
    input.response_format = { type: "json_object" };
  }
  let output: unknown;
  try {
    output = await env.AI.run(checked.model as keyof AiModels, input as never);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    const quota = isQuotaError(message);
    const latency = Date.now() - started;
    ctx.waitUntil(recordEvent(env.DB, day, { kind: "llm", route: "/edge/llm/v1/chat/completions", status: quota ? 429 : 502,
      outcome: quota ? "platform_quota" : "provider_error", latencyMs: latency, detail: message.slice(0, 120) }));
    if (quota) {
      // Mark the budget as spent so later calls fail fast until the daily reset.
      ctx.waitUntil(addCounter(env.DB, day, "neurons", "", budget).then(() => undefined));
      return errorResponse(429, "quota_exhausted", "The daily free inference allocation is used up; it resets at 00:00 UTC.", rid,
        { retry_after_s: retryAfter }, { "retry-after": String(retryAfter) });
    }
    return errorResponse(502, "provider_error", "The inference provider returned an error.", rid);
  }
  const latency = Date.now() - started;
  const result = normaliseOutput(output);
  const promptTokens = result.promptTokens ?? Math.ceil(promptChars / 4);
  const completionTokens = result.completionTokens ?? Math.ceil(result.text.length / 4);
  // Prefer the platform-reported neuron count; fall back to the catalog price estimate.
  const neurons = result.neurons ?? estimateNeurons(checked.model, promptTokens, completionTokens);
  ctx.waitUntil(Promise.all([
    addCounter(env.DB, day, "neurons", "", neurons),
    addCounter(env.DB, day, "llm_calls", "", 1),
    recordEvent(env.DB, day, { kind: "llm", route: "/edge/llm/v1/chat/completions", status: 200, outcome: "ok", latencyMs: latency,
      detail: `${checked.model}|${promptTokens}|${completionTokens}` }),
  ]).then(() => undefined));
  return json({
    id: `edge-${rid}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: checked.model,
    choices: [{ index: 0, message: { role: "assistant", content: result.text }, finish_reason: "stop" }],
    usage: { prompt_tokens: promptTokens, completion_tokens: completionTokens, total_tokens: promptTokens + completionTokens },
    edge: { latency_ms: latency, estimated_neurons: Number(neurons.toFixed(2)), neurons_reported: result.neurons !== undefined },
  }, 200, { "x-request-id": rid });
}

export async function inferenceSummary(env: Env): Promise<Record<string, unknown>> {
  const day = utcDay();
  const budget = limits(env).neuronBudget;
  const used = await getCounter(env.DB, day, "neurons");
  return {
    provider: "cloudflare-workers-ai",
    model: env.LLM_MODEL,
    configured: Boolean(env.LLM_PROXY_TOKEN),
    daily_neuron_budget: budget,
    neurons_used_today: Number(used.toFixed(1)),
    remaining_fraction: Number(Math.max(0, 1 - used / budget).toFixed(3)),
    quota_state: used >= budget ? "exhausted" : used >= budget * 0.8 ? "low" : "available",
    resets_in_s: secondsUntilUtcMidnight(),
  };
}
