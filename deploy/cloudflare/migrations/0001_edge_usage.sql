-- Edge usage accounting. No IP addresses, request bodies or clinical text are stored.
CREATE TABLE IF NOT EXISTS daily_counter (
  day TEXT NOT NULL,
  name TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  value REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (day, name, subject)
);

CREATE TABLE IF NOT EXISTS edge_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  route TEXT NOT NULL,
  status INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_edge_event_day_kind ON edge_event(day, kind);

-- Small key/value state, e.g. when a backend cold start was first observed.
CREATE TABLE IF NOT EXISTS edge_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
