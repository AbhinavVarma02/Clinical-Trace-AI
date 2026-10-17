# Safety and Privacy

Clinical-Trace AI is an educational research and engineering prototype that runs on synthetic records. It is not a medical device, is not clinically validated, and makes no regulatory or compliance claim (it is not "HIPAA compliant" or "certified"). The controls below are engineering safeguards for the prototype.

## Output boundaries

The agent must not diagnose, prescribe, recommend treatment, predict individual outcomes with certainty, invent observations or citations, or claim clinical deployment. These are enforced in code, independent of prompts:

| Risk | Control | Where |
| --- | --- | --- |
| Treatment / dosing / diagnosis requests | Refused before any tool runs (`clinical_decision`) | `src/safety/guards.py` |
| Requests for certainty about outcomes | Refused (`outcome_certainty`) | `guards.py` |
| Prompt injection in the question | Refused (`prompt_injection`) | `guards.py` |
| Instruction-like text inside records | Chunk flagged at indexing; flagged sentences excluded from answers; text withheld from the LLM; review recommended | `retrieval/index.py`, `agent/composer.py`, `agent/graph.py` |
| Other-patient references | Refused (`cross_patient`) using other patients' ids and names | `guards.py`, `agent/service.py` |
| Bulk / exfiltration requests | Refused (`data_exfiltration`) | `guards.py` |
| Direct identifiers | Refused (`sensitive_identifier`); none exist in the fixtures | `guards.py` |
| Invented citations or numbers, off-topic claims | Claim-level verification; failing claims removed; abstain if none survive | `src/safety/verifier.py` |
| Unsafe generated phrasing | Banned phrases and assertion patterns (shared with the v1 explainer) | `guards.py::check_output` |
| Missing evidence | Abstention with an explicit reason | `agent/composer.py`, `graph.py` |
| Contradictory records | Detected, every source cited, review recommended | `src/clinical/consistency.py` |
| Model applied out of scope | Model unavailable unless the record maps to its inputs; caveats always attached | `src/clinical/risk_bridge.py` |

Guard rules are pattern-based. They are tested (see `tests/test_safety_v2.py`) but they are not exhaustive; novel phrasings can evade them, which is why verification and patient binding are separate layers.

## Authorisation (policy-as-code)

- Central `PolicyEngine` in `src/safety/policy.py`; unknown personas, unknown actions, and patient actions outside the panel are denied.
- Roles: **clinician** (read, retrieve, run agent, submit reviews), **reviewer** (read, decide reviews; cannot run the agent), **admin** (audit, operations, evaluation; no patient data).
- Every decision, allowed or denied, is written to `audit_event` with persona, action, resource reference, and reason.
- Agent tools take the patient id from the authorised session, never from model output, and each tool call is re-authorised.
- Sessions are bound to persona + patient; reuse with another patient or persona fails.
- Runs are private to their persona; only reviewers may open another persona's run.
- Demo personas are chosen with a request header. This demonstrates enforcement; it is **not authentication**.

## Privacy

- All patient data is synthetic and tagged `urn:clinical-trace:data-origin|synthetic`. Patient ids must match `synthetic_*`.
- Agent traces, audit rows, and request logs store identifiers and timings only — never note text, question text in traces, or headers. Run results (which include the question and cited evidence) are stored for session history and review.
- No `.env` is read inside the container (`CLINICAL_TRACE_SKIP_DOTENV=1`); optional provider keys come from environment variables and are never logged or returned.
- Attachments: only `text/plain`, base64-validated, UTF-8, ≤ 64 KB, control characters stripped.

## Platform hardening

| Control | Detail |
| --- | --- |
| Input validation | Pydantic schemas with `extra="forbid"`, length and pattern limits |
| Request size | 32 KB body limit on `/api/v2` and `/fhir` |
| Rate limiting | Per-client token buckets (120/min; agent 20/min) |
| Timeouts / retries | Tool timeouts (5–15 s); FHIR client and LLM provider retries with backoff; LLM circuit breaker |
| Fail closed | Authorisation errors, retrieval isolation post-check, tool evidence for another patient |
| Container | Multi-stage build, non-root user, health check, no Node runtime |
| Secrets | `.env` git-ignored; tracked files scanned for token patterns in tests and CI |
| Dependency scanning | `npm audit` and `pip-audit` in CI |
| Headers | `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` |

## Human review

Outputs that include a model risk estimate, inconsistencies, injected text, partial verification, or an LLM fallback are flagged for review. Reviews bind to an artifact content hash; any revision creates a new version and supersedes earlier approvals. Submitters cannot approve their own artifacts.

## Retention

The SQLite database (`var/clinical_trace.db`) holds only synthetic data and demo activity. Deleting the file resets all runs, reviews, and audit history; the fixture is re-ingested on start. On the hosted demo the database lives in ephemeral container storage and resets on restart.

## Request and session hardening

The HTTP layer caps actual protected request bytes before parsing (including absent or understated Content-Length), enforces a body-arrival time budget, limits bucket memory, and logs rejected requests with server-generated request IDs and safe route categories. Rate-limit identity uses the ASGI peer. Configure trusted proxy addresses explicitly in Uvicorn; never accept arbitrary forwarded headers from public clients. Buckets remain process-local.

V2 schema errors use the error contract without echoing invalid input. Browser persona changes remount page state and scoped fetches hide stale data immediately; late agent responses from a previous persona cannot appear in the current context. Evidence drawers contain keyboard focus and restore the invoking focus on close. Durable checkpoints contain synthetic source state; diagnostic traces do not. See RELIABILITY.md for retention holds and restart constraints.
