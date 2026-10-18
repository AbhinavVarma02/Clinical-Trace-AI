// Edge configuration, bindings and the Workers AI price table used for budget accounting.

export interface Env {
  ASSETS: Fetcher;
  AI: Ai;
  DB: D1Database;
  API_LIMITER?: RateLimit;
  AGENT_LIMITER?: RateLimit;
  /** Origin of the Python/FastAPI backend, e.g. https://<space>.hf.space. Empty disables the gateway. */
  BACKEND_ORIGIN: string;
  /** Shared secret sent to the backend so it only accepts edge-mediated public traffic. */
  EDGE_BACKEND_SECRET?: string;
  /** Bearer token the backend uses to call the inference proxy. */
  LLM_PROXY_TOKEN?: string;
  LLM_MODEL: string;
  DAILY_NEURON_BUDGET: string;
  DAILY_AGENT_RUNS_GLOBAL: string;
  DAILY_AGENT_RUNS_PER_CLIENT: string;
  RELEASE: string;
}

export interface Limits {
  neuronBudget: number;
  agentRunsGlobal: number;
  agentRunsPerClient: number;
}

function positive(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function limits(env: Env): Limits {
  return {
    // Workers Free includes 10,000 neurons/day; stop well before the platform limit.
    neuronBudget: Math.min(positive(env.DAILY_NEURON_BUDGET, 8000), 9500),
    agentRunsGlobal: positive(env.DAILY_AGENT_RUNS_GLOBAL, 300),
    agentRunsPerClient: positive(env.DAILY_AGENT_RUNS_PER_CLIENT, 25),
  };
}

/** USD per million tokens from the Workers AI catalog (`wrangler ai models list`, 2026-10-17). */
export const MODEL_PRICES: Record<string, { input: number; output: number; jsonMode: boolean }> = {
  "@cf/qwen/qwen3-30b-a3b-fp8": { input: 0.0509, output: 0.335, jsonMode: false },
  "@cf/google/gemma-4-26b-a4b-it": { input: 0.1, output: 0.3, jsonMode: false },
  "@cf/mistralai/mistral-small-3.1-24b-instruct": { input: 0.351, output: 0.555, jsonMode: false },
  "@cf/meta/llama-3.3-70b-instruct-fp8-fast": { input: 0.293, output: 2.253, jsonMode: true },
  "@cf/meta/llama-3.1-8b-instruct-fp8": { input: 0.152, output: 0.287, jsonMode: false },
  "@cf/openai/gpt-oss-20b": { input: 0.2, output: 0.3, jsonMode: false },
  "@cf/zai-org/glm-4.7-flash": { input: 0.0605, output: 0.4, jsonMode: false },
};

/** One neuron is priced at $0.011 per 1,000 neurons. */
export const USD_PER_NEURON = 0.011 / 1000;

export function estimateNeurons(model: string, promptTokens: number, completionTokens: number): number {
  const price = MODEL_PRICES[model];
  if (!price) return Number.POSITIVE_INFINITY;
  const usd = (promptTokens * price.input + completionTokens * price.output) / 1_000_000;
  return usd / USD_PER_NEURON;
}

export function utcDay(now = new Date()): string {
  return now.toISOString().slice(0, 10);
}

export function secondsUntilUtcMidnight(now = new Date()): number {
  const next = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1);
  return Math.max(1, Math.ceil((next - now.getTime()) / 1000));
}
