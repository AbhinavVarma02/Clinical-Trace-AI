# Evaluation

Clinical-Trace AI has two independent evaluation tracks:

1. **Readmission model** (UCI Diabetes 130-US benchmark) — unchanged from v1; see [model_card.md](model_card.md).
2. **Agent platform** (this document) — the Clinical Case Review Agent and its retrieval index, evaluated on versioned synthetic cases.

## Method

| Aspect | Choice |
| --- | --- |
| Runner | `python -m evaluation.run_eval` (deterministic mode) and `--provider ollama --model <tag>` for local LLM runs |
| Cases | `evaluation/datasets/agent_dev_v1.jsonl` (20) and `agent_heldout_v1.jsonl` (22); `retrieval_v1.jsonl` (7 dev + 12 held-out queries) |
| Scoring | Deterministic rule checks only: expected outcome, refusal category, required/forbidden substrings, expected tools, expected source records, review flag, model availability. **No LLM judge and no human ratings.** |
| Thresholds | `evaluation/thresholds.json`, fixed on 2026-10-17 **before** the first run |
| CI gate | `tests/test_evaluation_gate.py` runs the deterministic evaluation and fails on any gate breach |
| Outputs | `evaluation/results/latest.json`, `llm_<model>.json`, `history.jsonl` (shown in the workspace Evaluation page) |

**Development vs held-out.** Development cases were written alongside the agent and the agent was iterated against them. Held-out cases were written before the first evaluation run and have not been used to change the system since; the one held-out failure below is reported as-is. The same author wrote both the system and the cases, so this is not an independent benchmark.

### Metric definitions

| Metric | Definition |
| --- | --- |
| Outcome accuracy | Share of cases whose outcome (`answered` / `abstained` / `refused` / authorisation error) matches the expectation |
| Answer correctness | Share of cases with required facts where *every* required substring appears in the verified answer (strict; paraphrases such as "July 5, 2025" for "2025-07-05" count as misses) |
| Tool selection | Share of cases where at least one expected tool was called |
| Evidence recall | Share of expected source records present in the evidence the agent gathered |
| Citation precision | Share of citations in returned claims that resolve to same-patient evidence from the same run |
| Citation coverage | Share of returned claims with at least one citation |
| Groundedness | Mean share of drafted claims that survived verification |
| Unsupported-claim rate | Rejected drafted claims / all drafted claims (LLM drafts and fallbacks both counted) |
| Safety adherence | Share of *critical* cases (refusals, injection, isolation, authorisation) that fully pass |
| Isolation / authorisation violations | Evidence or results from another patient; a forbidden request that succeeded |

## Results (dataset v1, thresholds v1.0)

Runs on 2026-10-17, Windows 11, Python 3.13, readmission model loaded. LLM runs used local Ollama (`qwen2.5:1.5b`, Q4_K_M; `qwen2.5:latest`, the 7B instruct build) at temperature 0. Cost: $0 (local inference). Latency is end-to-end agent time in-process.

### Agent (all 42 cases)

| Metric | Deterministic | qwen2.5 1.5B | qwen2.5 7B |
| --- | --- | --- | --- |
| Gate | **pass** | **fail** (answer correctness 0.478 < 0.70) | **pass** |
| Cases passed | 41 / 42 | 29 / 42 | 35 / 42 |
| Outcome accuracy | 1.000 | 0.975 | 1.000 |
| Answer correctness | 0.957 | 0.478 | 0.739 |
| Tool selection | 1.000 | 0.708 | 0.875 |
| Evidence recall | 0.941 | 0.794 | 0.824 |
| Citation precision / coverage | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 |
| Groundedness | 0.933 | 0.844 | 0.794 |
| Unsupported-claim rate (drafts) | 0.000 | 0.205 | 0.231 |
| LLM fallback rate | — | 0.333 | 0.167 |
| Safety adherence (critical) | 1.000 | 1.000 | 1.000 |
| Isolation / authorisation violations | 0 / 0 | 0 / 0 | 0 / 0 |
| Latency p50 / p95 | 15 ms / 23 ms | 1.04 s / 3.50 s | 2.71 s / 8.02 s |
| Tokens (prompt / completion) | — | 44,566 / 6,145 | 34,531 / 5,037 |

Split detail (deterministic): development 20/20; held-out 21/22 (answer correctness 0.923, evidence recall 0.889).

**Reading these numbers.**

- The verifier is doing real work: 20–23% of LLM-drafted claims were rejected (unsupported numbers, invented citations, or low lexical support), and those answers either lost the claim or fell back to the deterministic composer. Because only verified claims are returned, citation precision is 1.0 by construction — the informative figures are the unsupported-claim and fallback rates.
- Safety-critical behaviour (refusals, injection, isolation, authorisation) does not depend on the model: the guards and policy run before any model call, so it held at 1.0 for all three modes.
- The deterministic mode scores highest on these substring checks because its templates restate record values verbatim; it cannot answer questions outside its intent rules (those cases abstain).
- 1.5B is not acceptable under the pre-defined gate. 7B passes but at roughly 180× the deterministic latency on this machine.

### Failures (as measured)

| Case | Mode | Failure | Cause |
| --- | --- | --- | --- |
| ho-013 "What bleeding was documented in April?" | deterministic | correctness | Lexical mismatch: the query word "documented" matched "Plan documented by clinic" sections (BM25 rank 1–2), while the April discharge summary says "Nosebleed", not "bleeding", and was not in the top 5. Not tuned against (held-out). |
| dev-003, dev-001, ho-007 … | 7B | correctness | Paraphrased answers ("HbA1c has increased over time") without the specific values the strict check requires |
| dev-004, dev-005, dev-007 | 7B | tool selection | Model chose different tools than expected (e.g. prediction instead of encounters) |
| 13 cases | 1.5B | correctness / tools | Weak tool selection and paraphrase; see `evaluation/results/llm_qwen2.5_1.5b.json` |

### Retrieval (19 queries; relevance = source record)

| Mode | Recall@5 | Precision@5 | MRR@10 | nDCG@10 | p95 latency |
| --- | --- | --- | --- | --- | --- |
| BM25 | 0.904 | 0.497 | 0.921 | 0.903 | 2.5 ms |
| Dense (LSA) | 0.974 | 0.358 | 0.921 | 0.935 | 1.9 ms |
| Hybrid (RRF) | **0.974** | 0.358 | **0.947** | **0.951** | 1.8 ms |

Hybrid fusion improved recall and ranking over BM25 but **lowered precision@5** (0.497 → 0.358) because dense retrieval surfaces additional structured-fact chunks; with 1–4 relevant sources per query, precision@5 is bounded well below 1. Held-out hybrid: recall@5 0.958, MRR@10 0.958. Cross-patient isolation: 57 probes (every query run against every other patient), 0 violations.

The dense embedder is a classical TF-IDF + SVD (LSA) baseline. A comparison against neural general-purpose and biomedical encoders has **not** been run (roadmap CT-P13-T02).

## Regression rules

- Critical (all modes): isolation violations = 0, authorisation violations = 0, safety adherence = 1.0, schema validity = 1.0, unsafe outputs = 0, retrieval isolation violations = 0.
- Deterministic quality: outcome ≥ 0.90, correctness ≥ 0.85, tool selection ≥ 0.85, citation coverage = 1.0, evidence recall ≥ 0.80.
- LLM quality: outcome ≥ 0.80, correctness ≥ 0.70, citation coverage = 1.0.
- Hybrid retrieval: recall@5 ≥ 0.75, MRR@10 ≥ 0.60.

## Feedback loop

Run feedback (`POST /api/v2/agent/runs/{id}/feedback`) is stored with a structured category (`incorrect`, `unsupported`, `missing_evidence`, `unsafe`, `helpful`, `other`). It never changes agent or model behaviour automatically: a maintainer reviews it, adds a case to a **new** dataset version, re-runs the gate, and only then changes the system.

## Limitations

- Small, synthetic, single-author case set; passing it does not demonstrate clinical safety or generalisation.
- Substring scoring under-credits correct paraphrases and over-credits answers that happen to contain the strings.
- The lexical-support check in the verifier is a heuristic, not semantic entailment.
- LLM numbers are for one machine and one quantisation; they will differ on the free hosted tier.

## Readmission calibration and subgroup audit

See [READMISSION_ANALYSIS.md](READMISSION_ANALYSIS.md) and the aggregate reproducible artifact. Held-out Brier 0.133610, ECE 0.190108; mean score 0.302015 versus prevalence 0.111908. Scores are poorly calibrated; no retraining or threshold change was made.

## Durable-runtime inference revalidation

At committed source `6541d9b`, the already-downloaded local Ollama `qwen2.5:latest` (7B) was re-evaluated against all 42 locked cases without replacing historical reports. [llm_qwen2.5_7b_durable.json](../evaluation/results/llm_qwen2.5_7b_durable.json) records gate PASS, 35/42 cases, correctness 0.7391, unsupported draft-claim rate 0.2308 (rejected by verification), safety adherence 1.0 on the finite critical cases, and zero isolation/authorization violations. p50 2672.5 ms, p95 8018.02 ms; 34,513 prompt tokens and 5,014 completion tokens, no API charge. These are local hardware results, not free-CPU hosting latency or clinical validation. Lower LLM quality and fallback use remain visible; the 1.5B historical failure remains unchanged. No cloud-tagged Ollama model was called.

Human agreement remains unmeasured. A complete-pair annotation validator and agreement runner are provided in `evaluation/review_agreement.py`; see [REVIEW_VALIDATION_PROTOCOL.md](REVIEW_VALIDATION_PROTOCOL.md). Its artificial unit-test labels are not reviewer evidence.

## Hosted free inference: Cloudflare Workers AI (2026-10-18)

The public deployment uses Workers AI through the edge's authenticated OpenAI-compatible proxy ([ADR 0007](ADR/0007-public-edge-and-inference.md)). The same locked 42 cases, scoring and gate were used; the backend ran locally and called the proxy. Temperature 0; Qwen3 uses its documented `/no_think` switch.

| Model (Workers AI) | Gate | Passed | Correctness | Unsupported-claim rate | Tool selection | p50 / p95 | Neurons (42 cases) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Qwen3-30B-A3B, recorded at `d00dbc7` (current) | pass | 39/42 | 0.870 | 0.139 | 1.000* | 1.48 s / 4.54 s | 433.7 |
| Qwen3-30B-A3B, recorded at `759e55c` (superseded) | pass | 36/42 | 0.826 | 0.144 | see history | 1.08 s / 3.90 s | 358.5 |
| Qwen3-30B-A3B, exploratory (local proxy) | pass | 37/42 | 0.870 | 0.054 | 0.875 | 1.07 s / 3.90 s | about 380 |
| Qwen3-30B-A3B, exploratory (deployed public proxy) | pass | 37/42 | 0.870 | 0.049 | 0.833 | 1.32 s / 5.00 s | about 360 |
| Llama 3.3 70B fp8-fast, exploratory | pass | 37/42 | 0.826 | 0.039 | 0.917 | 3.70 s / 10.74 s | about 1,900 |
| Gemma 4 26B A4B, exploratory | pass | 37/42 | 0.783 | 0.000 | 0.875 | 9.21 s / 20.55 s | about 1,200 |

Only the recorded run is a committed artifact ([llm_cf_qwen_qwen3-30b-a3b-fp8.json](../evaluation/results/llm_cf_qwen_qwen3-30b-a3b-fp8.json)); exploratory runs used `--no-write` and are reported from their console output. Hosted inference is not bit-reproducible at temperature 0: identical code scored 36-37/42 and 0.049-0.144 unsupported-claim rate across runs, so the recorded (worse) run is the reference. All runs had safety adherence 1.0 and zero isolation or authorisation violations; unsupported drafted claims were removed by verification before any answer was returned. Qwen3 was selected for the best correctness at the lowest latency and cost (about 9 neurons per run, roughly 850 runs inside the 8,000-neuron daily cap). These are synthetic, rule-scored results, not clinical validation.

\* From `d00dbc7` the rule-based plan is the floor of every LLM plan (the model may add tools, never drop intent-required ones) and the composer may use 1,000 tokens. This followed a public-path run where the LLM planner chose only the timeline tool for a journey summary, the composer's long JSON failed to parse and the deterministic fallback abstained. Tool-selection correctness is 1.000 partly by construction under this rule. The fix was evaluated on the same locked cases; the earlier run remains in `history.jsonl`.

In the public deployment the completions are relayed by the edge (`src/agent/relay.py`): the backend emits the same chat-completion request on the streamed run and the edge answers it on Workers AI, so model, prompts and budgets are identical to the measured configuration.

## Agent reliability continuation

The unreleased continuation at `23ad61c` adds a capped, repeatable six-question
real-model workload and narrowly scoped composer/verifier regressions. Every
development candidate and resource-limited control is retained. The original
42-case scoring and thresholds are unchanged: final offline gate 41/42, real-model
gate 37/42, both passing; finite critical safety checks pass. Full results and
limitations: [AGENT_RELIABILITY.md](AGENT_RELIABILITY.md). No production deployment
or clinical-validation claim is made.
