# ADR 0009: A separate Beta channel, starting with local data sources

**Status:** M2-S1 (local data-source registry) accepted and implemented on the local branch `feature/beta` (2026-11-06). The rest of the Beta channel is a provisional roadmap; deployment, cloud resources and public Epic content are not authorised.

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

**Consequences.**

- Default behaviour is unchanged. The default-mode API responses, 97 of them including the FHIR routes, matched a baseline captured on `028e609`. The Stable edge allowlist does not forward `X-Data-Source`.
- The application imports the Epic adapter only when an Epic source is configured.
- Not yet built: the Beta UI, edge configuration, Beta container, Epic publication bundles and deployment. Each needs its own approval.
- Remaining Epic limitations are listed in [EPIC_SANDBOX.md](../EPIC_SANDBOX.md#limitations-and-pending-decisions).
