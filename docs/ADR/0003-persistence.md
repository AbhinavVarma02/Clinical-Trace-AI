# ADR 0003: SQLite now, PostgreSQL/pgvector later

**Status:** accepted (2026-10-17)

**Context.** The target design names PostgreSQL + pgvector. The corpus is under 200 chunks, the demo must cost $0, and tests must run without external services.

**Decision.** Use SQLite (WAL mode) through a thin wrapper with forward-only, numbered migrations written in portable SQL. Keep embeddings in memory (rebuilt on ingestion) rather than in the database.

**Consequences.** Zero setup on Windows, WSL2, Docker, and CI. No multi-writer scaling and no vector search in the database. Moving to PostgreSQL means re-implementing `Database` against a PostgreSQL driver and porting the application migrations; pgvector becomes worthwhile when the corpus no longer fits comfortably in memory.

## Durable execution update (2026-10-17)

LangGraph SQLite checkpoint tables now share the file through a separate connection and pinned adapter. Application migration 5 adds execution/idempotency controls. Use one application worker per SQLite file. Retention removes eligible checkpoints and run records in one app transaction. Checkpoints are private source-bearing state, distinct from text-free observability. PostgreSQL remains optional pending measured need.

Migration 6 persists text-free retrieval manifests/chunk provenance and evaluation aggregates. The checkpoint adapter is pinned at patched version 3.1.1.
