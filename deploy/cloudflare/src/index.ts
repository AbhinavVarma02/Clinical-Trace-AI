// Clinical-Trace AI public edge (Cloudflare Workers Free plan).
//
// Static workspace assets are served by the platform without invoking this script. The script
// handles /api/* (guarded gateway to the Python backend) and /edge/* (status, metrics, inference).

import type { Env } from "./config";
import { handleApi } from "./gateway";
import { errorResponse, requestId } from "./http";
import { handleChatCompletions } from "./inference";
import { handleMetrics, handleStatus } from "./status";
import { pruneEvents } from "./store";

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const rid = requestId(request);
    try {
      if (url.pathname.startsWith("/api/")) return await handleApi(request, env, ctx, rid);
      if (url.pathname === "/edge/status" && request.method === "GET") return await handleStatus(env, ctx);
      if (url.pathname === "/edge/metrics" && request.method === "GET") return await handleMetrics(env);
      if (url.pathname === "/edge/llm/v1/chat/completions" && request.method === "POST") {
        return await handleChatCompletions(request, env, ctx, rid);
      }
      if (url.pathname.startsWith("/edge/")) return errorResponse(404, "not_found", "Unknown edge route.", rid);
      return env.ASSETS.fetch(request);
    } catch (err) {
      console.error(JSON.stringify({ rid, path: url.pathname, error: err instanceof Error ? err.name : "unknown" }));
      return errorResponse(500, "edge_error", "The edge could not process this request.", rid);
    }
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(pruneEvents(env.DB));
  },
} satisfies ExportedHandler<Env>;
