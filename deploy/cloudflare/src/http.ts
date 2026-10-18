// Response helpers. Error bodies match the backend envelope: {"error": {code, message, request_id}}.

export function requestId(request: Request): string {
  const incoming = request.headers.get("x-request-id");
  return incoming && /^[A-Za-z0-9-]{8,64}$/.test(incoming) ? incoming : crypto.randomUUID();
}

export function json(body: unknown, status = 200, headers: HeadersInit = {}): Response {
  const out = new Headers(headers);
  out.set("content-type", "application/json; charset=utf-8");
  out.set("cache-control", "no-store");
  return new Response(JSON.stringify(body), { status, headers: out });
}

export function errorResponse(
  status: number,
  code: string,
  message: string,
  rid: string,
  extra: Record<string, unknown> = {},
  headers: HeadersInit = {},
): Response {
  const out = new Headers(headers);
  out.set("x-request-id", rid);
  return json({ error: { code, message, request_id: rid, ...extra } }, status, out);
}

/** Hex SHA-256 of `value` (used for salted, non-reversible client identifiers). */
export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Constant-time comparison for shared secrets. */
export function safeEqual(a: string, b: string): boolean {
  const left = new TextEncoder().encode(a);
  const right = new TextEncoder().encode(b);
  let diff = left.length ^ right.length;
  for (let i = 0; i < Math.max(left.length, right.length); i++) diff |= (left[i] ?? 0) ^ (right[i] ?? 0);
  return diff === 0;
}

/**
 * Daily-rotating client key derived from the connecting IP. The raw address is never stored or
 * forwarded; the salt changes every UTC day so keys cannot be joined across days.
 */
export async function clientKey(request: Request, salt: string, day: string): Promise<string> {
  const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
  return (await sha256Hex(`${salt}|${day}|${ip}`)).slice(0, 24);
}
