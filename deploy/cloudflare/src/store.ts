// D1-backed daily counters and privacy-safe edge events (route templates, status, timing only).

export interface EdgeEvent {
  kind: "api" | "agent" | "llm" | "wake";
  route: string;
  status: number;
  outcome: string;
  latencyMs: number;
  detail?: string;
}

export async function addCounter(db: D1Database, day: string, name: string, subject: string, amount: number): Promise<number> {
  const row = await db
    .prepare(
      "INSERT INTO daily_counter(day, name, subject, value) VALUES (?1, ?2, ?3, ?4) " +
        "ON CONFLICT(day, name, subject) DO UPDATE SET value = value + excluded.value RETURNING value",
    )
    .bind(day, name, subject, amount)
    .first<{ value: number }>();
  return row?.value ?? amount;
}

export async function getCounter(db: D1Database, day: string, name: string, subject = ""): Promise<number> {
  const row = await db
    .prepare("SELECT value FROM daily_counter WHERE day = ?1 AND name = ?2 AND subject = ?3")
    .bind(day, name, subject)
    .first<{ value: number }>();
  return row?.value ?? 0;
}

export async function recordEvent(db: D1Database, day: string, event: EdgeEvent): Promise<void> {
  await db
    .prepare("INSERT INTO edge_event(ts, day, kind, route, status, outcome, latency_ms, detail) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)")
    .bind(new Date().toISOString(), day, event.kind, event.route, event.status, event.outcome, Math.round(event.latencyMs), event.detail ?? null)
    .run();
}

export async function pruneEvents(db: D1Database, keepDays = 14): Promise<void> {
  const cutoff = new Date(Date.now() - keepDays * 86_400_000).toISOString().slice(0, 10);
  await db.batch([
    db.prepare("DELETE FROM edge_event WHERE day < ?1").bind(cutoff),
    db.prepare("DELETE FROM daily_counter WHERE day < ?1").bind(cutoff),
  ]);
}
