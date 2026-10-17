# ADR 0002: Safety boundaries live in code, not prompts

**Status:** accepted (2026-10-17)

**Context.** Language models do not reliably follow instructions such as "only cite the evidence" or "never diagnose". The demo must stay safe in deterministic mode and with small local models.

**Decision.**
1. Classify questions with deterministic guards before any tool runs.
2. Bind the patient id to the authorised session; tools accept no patient argument from the model.
3. Re-authorise every tool call through the central policy.
4. Verify each generated claim against evidence collected in the same run (citations, numbers, recomputed derived values, lexical support, unsafe-phrase rules). Remove failing claims; abstain if none remain.
5. Treat retrieved text as data: flag instruction-like content and never pass it to the model.

**Consequences.** Safety results are identical across inference modes (evaluation: safety adherence 1.0 in all three). Some valid LLM paraphrases are rejected (unsupported-claim rate 20–23%), traded for not returning unsupported text.
