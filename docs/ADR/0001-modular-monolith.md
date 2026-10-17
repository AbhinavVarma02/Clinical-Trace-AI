# ADR 0001: Modular monolith

**Status:** accepted (2026-10-17)

**Context.** The target architecture lists many logical layers (UI, API, policy, orchestration, retrieval, ML, review, persistence, evaluation). The demo must run in one free container, and nothing needs independent scaling yet.

**Decision.** One FastAPI process with separate Python packages per layer (`src/clinical`, `src/retrieval`, `src/agent`, `src/safety`, `src/governance`, `src/persistence`). Layers communicate through function calls with typed data, and the API layer holds no business logic.

**Consequences.** Simple deployment and testing; components cannot scale separately. Splitting a service out later requires replacing calls with network clients at package boundaries, which the boundaries are drawn to allow.
