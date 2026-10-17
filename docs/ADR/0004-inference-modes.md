# ADR 0004: Deterministic and LLM inference modes, always labelled

**Status:** accepted (2026-10-17)

**Context.** The public demo needs real agent behaviour at $0, and free hosted inference has quotas and outages. Scripted output must never be presented as model output.

**Decision.** Two modes behind the same LangGraph graph:
- *Deterministic* (default): rule-based intent planning, template and extractive composition. No model is called and responses say so.
- *LLM*: any OpenAI-compatible endpoint (local Ollama measured) for tool selection and drafting, followed by the same verification; failed drafts fall back to deterministic composition, which is recorded and flags the answer for review.

**Consequences.** CI and the free demo are reliable without a model. LLM quality is measured, not assumed: 7B passed the gate, 1.5B did not.
