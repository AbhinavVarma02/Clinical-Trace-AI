# ADR 0009: A separate Beta channel, starting with local data sources

**Status:** M2-S1 (local data-source registry) accepted and implemented on `feature/beta` (2026-11-06). Amended 2026-11-06 (America/New_York): controlled owner-only real-time Epic FHIR R4 sandbox retrieval is a **mandatory M2.3 target**, not implemented. Implementation, live calls, deployment, cloud resources and public Epic content are not authorised by this documentation amendment.

**Context.** The Stable public demo (the Cloudflare edge plus the co-hosted backend, [ADR 0007](0007-public-edge-and-inference.md)) must stay unchanged. The Epic sandbox adapter ([ADR 0008](0008-epic-sandbox-source-adapter.md)) writes immutable, single-patient snapshots that only the command line reads. A separately deployed Beta should later reuse the application with Epic-derived test records. Before that, the application must be able to serve more than one data source without changing what it serves by default. It runs one global runtime, its personas are pinned to the four synthetic patients, and its status reported a fixed synthetic origin.

**Decision.**

- **Branching.** Beta work lives on `feature/beta`, branched from the verified Epic M1 commit `028e609`. Epic Draft PR #2 is not changed or merged. Commits, pushes, PRs and merges each need the owner's approval.
- **Data sources (M2-S1).** A registry (`src/sources.py`) holds one isolated runtime per configured source. `CLINICAL_TRACE_DATA_SOURCES` is unset by default, which means the synthetic fixture only, built exactly as before. Requests choose a source with the optional `X-Data-Source` header. Without it they get the synthetic source, which must be listed first.
- **Isolation.** Each source has its own database, retrieval index, agent, reviews, audit log and request log. Persona panels for an Epic source come from its manifest. A patient id may appear in one source only. A patient, run or review of one source is unreachable through another.
- **Snapshots stay read-only.** An Epic source is built from the snapshot's verified bundle into an in-memory database, so serving never writes into the snapshot.
- **Deterministic only.** Epic sources refuse to start when a language-model provider or LangSmith tracing is configured. The language-model composer does not carry coverage notes, and tracing would upload record content.
- **Truthful reporting.** Status reports each source's origin, MOCK/LIVE mode and retrieval time. The provenance route adds the Epic lineage and field status. A coverage route reports what the source declares it imported. The synthetic offline evaluation is not presented as evidence for other sources.
- **Fail closed.** Invalid configuration, a missing, misplaced or modified snapshot, or repeated patients stop startup. An unknown or malformed `X-Data-Source` returns 400.
- **Testing.** Committed tests use only the synthetic fixture and the project's MOCK Epic-shaped fixtures. Private LIVE snapshots stay local, git-ignored and out of all artifacts.

**Owner decisions recorded 2026-11-06.** Approved:
- D1 (branching);
- D4 (Beta deterministic-only);
- D6, limited to local synthetic and MOCK Epic testing;
- D9 (this ADR; other records in existing documents).

D8 (phased roadmap) is accepted provisionally as a roadmap, not as deployment authorisation. Deferred to their phases and not authorised:
- D2 (Beta backend host);
- D3 (Cloudflare account placement);
- D5 (public Epic content);
- D7 (Beta UI approach).

**Owner amendment: mandatory M2.3 capability (2026-11-06 America/New_York).** The original M2-S1 decision and historical approvals above remain intact. The requirement below supersedes treating live Beta retrieval as optional or limiting Beta to pre-imported snapshots. Delivery is mandatory; implementation details and execution approvals remain gated.

- **Target architecture:** Beta UI → Approved synthetic patient selection → Owner-authorized Epic refresh → Secure backend OAuth → Live Epic FHIR sandbox API → Atomic validated import → Existing Clinical-Trace evidence engine → Verified answers, citations and human review.
- **Restricted scope:** initially owner-only, read-only, approved synthetic developer-sandbox patients. Backend owner authentication and per-operation authorisation are required; Epic application OAuth and demo persona headers do not authenticate the owner. No unrestricted public queries, real hospital data or production Epic integration.
- **Reuse and gaps:** reuse M1 OAuth/key tooling, pinned client, mapping/validation, provenance and content-free telemetry, plus M2-S1 isolation and the existing evidence/review engine. Add the owner identity/patient allowlist, refresh budgets, complete coverage contract, staged validation, atomic active-generation promotion, runtime version pinning and explicit rollback. Existing immutable snapshot creation is not atomic promotion; the startup registry is not a refresh service.
- **Truthfulness:** show source/mode, snapshot version, retrieval timestamps, last attempt/success, resource counts and import coverage. Distinguish not requested, not imported, returned none, failed and unresolved references. Preserve clinical dates. A fresh API request may return unchanged sandbox data; it does not imply continuously changing real-world clinical records. Cached snapshots retain explicit cache labels and original timestamps, with no silent fallback.
- **Security and isolation:** protect signing keys/tokens on the backend; enforce refresh rate/concurrency/resource budgets and minimal audit without credentials or unnecessary clinical content. Keep Beta infrastructure completely separate from Stable. Security/red-team tests use local MOCK services and synthetic fixtures, never Epic infrastructure; bounded live functional verification needs separate approval.
- **Sequence:** M2.2-d/e/f remain the next clinical workstreams; M2.3 delivers controlled live backend retrieval, M2.4 integrates UI controls/status/provenance, M2.5-M2.7 cover isolated hosting, security verification, deployment and release acceptance. Public live refresh, hosting choices, Cloudflare account selection and Epic data publication each require independent approval.

Prerequisites, risks and acceptance are in [EPIC_SANDBOX.md](../EPIC_SANDBOX.md#m23-mandatory-controlled-real-time-retrieval); implementation increments in [IMPLEMENTATION_ROADMAP.md](../IMPLEMENTATION_ROADMAP.md#beta-roadmap-mandatory-real-time-epic-sandbox-retrieval). This amends the existing ADR; no additional ADR was created.

**Consequences.**

- Default behaviour is unchanged. The default-mode API responses, 97 of them including the FHIR routes, matched a baseline captured on `028e609`. The Stable edge allowlist does not forward `X-Data-Source`.
- The application imports the Epic adapter only when an Epic source is configured.
- Not yet built: owner authentication, the M2.3 live refresh/promotion service, the Beta UI, isolated edge/container configuration and deployment. Public Epic publication is a separate approval, not an acceptance prerequisite for owner-only Beta.
- Remaining Epic limitations are listed in [EPIC_SANDBOX.md](../EPIC_SANDBOX.md#limitations-and-pending-decisions).
