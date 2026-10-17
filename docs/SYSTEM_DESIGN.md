# System Design

## Agent run lifecycle

```text
POST /api/v2/agent/runs {patient_id, question, session_id?}
  1. policy.require(persona, agent:run, patient)          → 403 if outside panel (audited)
  2. session bound to (persona, patient); mismatch        → 403 session_mismatch
  3. agent_run row inserted (status=running)
  4. LangGraph invoke (thread_id = run_id, recursion_limit = 40)
       guard        classify_question → refuse ⇒ finalize
       plan         deterministic rules or LLM tool selection (validated, sanitised args)
       act ×N       one tool per step, N ≤ 8; each: schema check → policy → timeout → evidence
       compose      LLM draft (first attempt in LLM mode) or deterministic templates/quotes
       verify       claim-level checks; failed LLM draft ⇒ compose again deterministically (once)
       review_gate  flags: partial verification, inconsistencies, injected text, model risk, LLM fallback
       finalize     outcome = refused | answered | abstained
  5. result shaped (claims, evidence, tool calls, trace, inference label) and persisted
```

Cancellation: every node checks a per-run event (`POST /agent/runs/{id}/cancel`). Exceptions inside the graph produce `outcome=error` with only the exception class recorded.

### Graph state (`src/agent/graph.py::AgentState`)

`run_id, question, patient_id, other_patient_markers, guard, plan, step, results{tool→ToolResult}, evidence{id→EvidenceItem}, draft, verification, compose_attempts, fallback_used, llm_attempt, review, outcome, trace[]`

### Evidence identifiers

| Kind | Id format | Example |
| --- | --- | --- |
| Structured resource version | `{resource_id}@v{version}` | `p004-m1@v2` |
| Document section chunk | `{document_id}#s{n}@v{version}` | `p001-d3#s2@v1` |
| Completeness check | `check:{category}:{n}` | `check:unit_missing:3` |
| Consistency check | `check:inconsistency:{n}` | `check:inconsistency:1` |
| Model output / SHAP | `model:{encounter}` / `shap:{encounter}` | `model:p001-e4` |

Every item carries `source_ref` and `source_version`, so any claim can be traced to an exact FHIR resource version.

### Verification rules (`src/safety/verifier.py`)

A claim is accepted only if all hold:

1. at least one citation; every citation exists in this run's evidence and belongs to the session patient;
2. every number in the claim appears in the cited evidence text, or is the result of a declared computation (`days_between`, `difference`, `count`) that the verifier recomputes from cited inputs;
3. ≥ 60% of the claim's content words (after stop-word, synonym, and template-word removal) occur in the cited evidence;
4. no banned phrase or unsafe clinical-assertion pattern (shared with the v1 LLM explainer).

## Data model (SQLite, `src/persistence/db.py`)

| Table | Purpose |
| --- | --- |
| `ingestion_run`, `ingestion_issue` | One row per ingestion with counts; validation issues per resource |
| `fhir_resource` | `(type, id, version)` primary key, `patient_id`, normalised `last_updated`, SHA-256 `content_hash`, JSON `payload`, `ingestion_run_id`, `is_current` |
| `agent_session`, `agent_run` | Sessions bound to persona + patient; run question, outcome, result JSON, identifier-only trace, latency |
| `artifact`, `review`, `review_event` | Versioned review artifacts with content hashes; review state machine and history |
| `audit_event` | Policy decisions and governance actions (identifiers only) |
| `feedback` | Structured run feedback |
| `request_log` | Method, route template, status, latency for `/api/v2` and `/fhir` |

### Ingestion semantics

- Same `(type, id, version)` + same hash → duplicate (no-op). Same key + different hash → rejected as conflict.
- A higher version becomes current; history is kept and readable (`/fhir/r4/{type}/{id}/_history/{v}`).
- Timestamps are normalised to UTC; the original value stays in the payload.
- After an ingestion that inserted rows, the retrieval index rebuilds and its version hash changes, invalidating the cache.

## Review state machine

```text
submit ─> pending_review ──approve──> approved ──(artifact revised)──> superseded
              │   ▲                                   new version ─> pending_review
              │   └─ needs_revision (comment required)
              └─reject (comment required)─> rejected
```

Decisions must present the artifact hash the reviewer saw; a mismatch returns 409. Submitters cannot decide their own reviews. `approval_valid` is true only while the approved hash equals the current artifact hash.

## Retrieval

- Chunking: notes split at section headers (`Assessment:`, `Plan at discharge:` …); structured resources become one-sentence fact chunks.
- Lexical: BM25 (k1 = 1.4, b = 0.75) with clinical synonym normalisation.
- Dense: TF-IDF word (1–2) + char_wb (3–5) n-grams → truncated SVD (96 dims) → L2 normalised; fitted on the full synthetic corpus, scored only within the patient partition.
- Fusion: reciprocal rank fusion, k = 60; BM25 contributes only chunks with a positive score.
- Filters: inclusive date range, source-type prefix. Cache: LRU 256 keyed by query, filters, and index version.

## Telemetry

Request middleware assigns `X-Request-ID`, applies per-client token buckets (120 req/min; 20 agent runs/min, configurable) and a 32 KB body limit, and logs route templates and latency. Agent traces store node name, duration, tool name, success, evidence count, and error type — never record text. `/api/v2/ops/metrics` computes p50/p95 from these rows.
