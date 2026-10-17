# Engineering Evidence

Each claim made in the README maps to code, a test, and where applicable a measured artifact. Commands assume the repository root.

| Claim | Implementation | Verification |
| --- | --- | --- |
| Synthetic records are unmistakably synthetic and valid FHIR (supported subset) | `src/clinical/synthetic.py`, `fhir_models.py` | `tests/test_fhir_data.py::test_every_resource_is_valid_and_tagged_synthetic`, `::test_committed_fixture_matches_generator` |
| Ingestion is idempotent, versioned, and rejects conflicting versions | `src/clinical/store.py` | `test_ingestion_is_idempotent`, `test_conflicting_version_is_rejected`, `test_new_version_becomes_current_and_history_is_kept` |
| FHIR client pages, retries, and rejects cross-patient results | `src/clinical/fhir_client.py` | `test_http_client_paginates_and_pulls_patient`, `test_http_client_rejects_cross_patient_results`, `test_http_client_retries_then_fails` |
| Mock FHIR server is read-only and panel-scoped | `api/fhir.py` | `tests/test_api_v2.py::test_fhir_server_read_search_and_scope` |
| The readmission model is not applied to ineligible records | `src/clinical/risk_bridge.py` | `test_risk_bridge_eligibility`, `test_model_unavailable_is_explicit` |
| Retrieval never crosses patients | `src/retrieval/index.py` | `tests/test_retrieval_v2.py::test_results_never_cross_patients` (24 parametrised cases, each across BM25, dense, and hybrid modes); evaluation isolation probes: 57, 0 violations |
| Hybrid retrieval quality | `src/retrieval/` | `evaluation/results/latest.json` → recall@5 0.974, MRR@10 0.947 |
| Tools are patient-bound and re-authorised | `src/agent/tools.py` | `test_tools_reject_patient_arguments_and_unauthorised_calls` |
| Tool timeouts produce structured errors | `run_tool` | `test_tool_timeout_is_structured` |
| Unsafe and cross-patient questions are refused before tools run | `src/safety/guards.py`, `graph.py` | `test_unsafe_questions_are_refused_before_tools`, `tests/test_safety_v2.py::test_guard_refusals` |
| Injected record text is not followed | `guards.flag_content`, `composer._extract_claims` | `test_injected_record_text_is_not_followed` |
| Every returned claim cites same-patient evidence from the same run | `src/safety/verifier.py` | `_assert_grounded` in `tests/test_agent_v2.py`; evaluation citation precision 1.0 |
| Hallucinated LLM claims are removed and fall back | `graph.py` verify → compose | `test_hallucinated_llm_answer_is_rejected_and_falls_back` |
| Real LLM inference works and is measured | `src/agent/llm.py` | `evaluation/results/llm_qwen2.5_latest.json`, `llm_qwen2.5_1.5b.json` |
| Provider outages degrade safely | `CircuitBreaker`, planner fallback | `test_provider_outage_degrades_to_deterministic`, `test_circuit_breaker_opens_after_failures` |
| Sessions are isolated per persona and patient | `src/agent/service.py` | `test_session_is_bound_to_persona_and_patient` |
| Default-deny policy with audit | `src/safety/policy.py`, `governance/audit.py` | `test_default_deny`, `test_policy_matrix`, `test_policy_engine_audits_every_decision` |
| Review approval binds to an exact artifact version | `src/governance/review.py` | `test_review_workflow_binds_to_artifact_hash` |
| Rate and size limits | `api/middleware.py` | `test_request_size_and_rate_limits` |
| Traces contain no record text | `graph.traced`, `ToolResult.summary` | `test_trace_contains_no_record_text` |
| Evaluation gate blocks regressions | `evaluation/run_eval.py`, `thresholds.json` | `tests/test_evaluation_gate.py` |
| v1 behaviour preserved | unchanged `src/`, `api/routes.py`, `dashboard/` | original 54 tests; `test_v1_endpoints_still_work` |
| Workspace renders real API data and labels inference mode | `frontend/workspace/` | `workspace/__tests__/workspace.test.tsx`; browser checks (desktop, 375 px mobile, light/dark) |

## Reproduce

```bash
pytest tests/ -q
python -m evaluation.run_eval
python -m evaluation.run_eval --provider ollama --model qwen2.5:7b     # needs a local Ollama model
cd frontend && npm ci && npm run check
```
