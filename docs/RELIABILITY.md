# Durable execution and local reliability

The clinical agent now uses the pinned LangGraph SQLite saver for node checkpoints. File-backed deployments retain state across process restarts; `:memory:` tests remain ephemeral. The saver has a separate connection to the same database, its own schema, and an explicit application-dataclass deserialization allowlist with pickle disabled. Checkpoint state contains synthetic source content and is private runtime state, never a public diagnostic trace.

## Controls and recovery

- One Uvicorn worker per SQLite database. Start with `uvicorn api.main:app --workers 1`; multiple application processes sharing the same DB are unsupported. Startup marks old running records interrupted and does not replay them automatically.
- `GET /api/v2/agent/runs/{run_id}/status` is owner-only. `POST .../{run_id}/resume` permits interrupted/failed runs, at most three times. Resume preserves completed LangGraph work and rechecks persona/patient permissions, application-source fingerprint, model/provider configuration and current FHIR/index content hashes. A changed dependency requires a fresh run. Legacy runs without checkpoints cannot resume.
- `Idempotency-Key` on `POST /api/v2/agent/runs` is scoped to the persona. Exact retries return the stored result. A different patient/question/session conflicts with HTTP 409. An unfinished run requires status inspection and explicit recovery.
- Cancellation is owner-only, persisted, checked between graph nodes, and cannot affect another clinician's run even if both can read the patient. Cancellation cannot forcibly interrupt a running Python function or provider request; tool/provider timeouts still bound those calls.
- Agent admission defaults to four active executions (`CLINICAL_TRACE_MAX_AGENT_RUNS`). Saturation/shutdown returns 503 with Retry-After. Eight tool slots include timed-out work until the underlying worker exits, so an unbounded task queue cannot accumulate. Provider half-open recovery permits one probe.
- The 180-second execution deadline is checked between nodes, alongside per-tool/provider timeouts and existing graph/call budgets. It is not a hard process-kill deadline.
- Shutdown stops admission, requests cooperative cancellation, drains for up to ten seconds and closes provider/checkpoint/app connections only when drained. A stuck worker leaves connections to process teardown rather than closing a database in use. Hard process termination is recovered on the next start; in-flight read-only tools may need replay if their completion was never checkpointed.

## Retention

Run `python -m src.persistence.retention --database var/clinical_trace.db` to preview aggregate pruning counts. `--apply` explicitly applies the policy; defaults are 30 days for terminal unreviewed runs and their feedback/checkpoints, seven days for request logs and 90 days for audit events. Override using `--run-days`, `--log-days`, `--audit-days` (minimum one day). Active/interrupted runs and every run referenced by a review artifact are held. Clinical resources, review artifacts/decisions and evaluation results are retained. Pruning is transactional; no automatic deletion is enabled. SQLite deletion is logical deletion, not forensic secure erasure; backups/filesystem copies need a separate policy.

## Evidence and limits

`tests/test_agent_recovery.py` interrupts the real graph after a real tool, closes/reopens the database/runtime, and resumes without repeating the completed tool. It also tests owner isolation, idempotency, admission and cooperative shutdown. `tests/test_retention.py` tests dry runs, active/review holds and atomic deletion of file-backed checkpoints. These recovery tests are deterministic local integration evidence. Separate actual 7B inference was revalidated at 6541d9b; see EVALUATION.md.

[performance_local.json](../evaluation/results/performance_local.json) records a measured 40-request workload at each offered concurrency (1, 4, 8). At concurrency four, all 40 requests answered with no isolation violations; p50 189.6 ms, p95 248.9 ms and successful throughput 20.9 requests/s. At concurrency eight, 28/40 requests received capacity rejection (503), and 12 answered with no isolation violations. Capacity rejection is reported as an HTTP error, not hidden as success. This in-process TestClient benchmark includes routing/middleware/checkpoint/database/graph work, but excludes TCP/TLS, browsers, containers and live LLM inference. Python allocation profiling is measured separately and does not represent native or total process memory. Rerun `python -m evaluation.benchmark --requests 40`; timings vary by hardware and load. Local Docker image/cold-start measurements remain blocked by Docker Desktop/WSL integration.

## Verified streaming and local traces

`POST /api/v2/agent/runs/stream` is the SSE alternative to the original JSON endpoint. It streams only real node/tool progress metadata, followed by one schema-validated result after claim verification. It never streams unverified draft tokens. Authorization and initial capacity/conflict errors retain HTTP status codes; errors after headers are explicit SSE error events. Disconnects request cooperative cancellation; completed or active run state remains inspectable. The React agent consumes this stream and offers cancellation once the real run ID arrives. The client rejects interrupted streams rather than treating partial data as an answer. Existing JSON clients continue to work.

Set `CLINICAL_TRACE_OTEL_FILE=var/traces/spans.jsonl` to opt into a local OpenTelemetry SDK exporter. No network collector or paid service is enabled. HTTP and graph-node spans share a trace ID (`X-Trace-ID` when enabled); HTTP logs retain request IDs, safe route templates and timings. Export attributes are allowlisted, and exception messages/events are disabled. `tests/test_observability.py` verifies correlation and absence of question/source/exception text. The private trace file must be rotated/removed under an operator-selected retention policy; the SQLite pruning CLI does not manage external trace files. No exporter means no trace file by default.

## Composition and verification follow-up

The unreleased continuation at `23ad61c` adds a capped, repeatable six-question
real-model workload and narrowly scoped composer/verifier regressions. Every
development candidate and resource-limited control is retained. The original
42-case scoring and thresholds are unchanged: final offline gate 41/42, real-model
gate 37/42, both passing; finite critical safety checks pass. Full results and
limitations: [AGENT_RELIABILITY.md](AGENT_RELIABILITY.md). No production deployment
or clinical-validation claim is made.
