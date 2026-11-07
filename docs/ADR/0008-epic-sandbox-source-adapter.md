# ADR 0008: Epic sandbox records enter through an isolated, provenance-preserving adapter

**Status:** accepted for internal M1 (2026-11-05); amended 2026-11-06 (see the end). Application snapshot serving (M2-S1) and the mandatory owner-only live retrieval target (M2.3, not implemented) are recorded in [ADR 0009](0009-beta-channel-and-data-sources.md). Public exposure still needs an independent decision.

**Context.** The platform reads a generated synthetic FHIR fixture. The owner has registered a read-only Backend Systems app on the Epic on FHIR public R4 sandbox, which holds only test patients. Raw Epic resources do not meet the platform's FHIR contract:

- Epic ids are long and unconstrained.
- Version and last-update metadata may be absent.
- Medication may be a reference rather than a code.
- Notes are HTML or RTF stored in `Binary`.
- Condition onsets can be partial dates.
- Local codings may come first.

Several existing views also assume the fixture's shapes. One example is that medication views use `meta.lastUpdated` as a medication date.

**Decision.**

- **Package.** A new package, `src/interop/epic/`, is used only from a local command line. Nothing in the running application imports it.
  - Placing it outside `src/clinical/` keeps the agent's `execution_revision` unchanged.
- **Authentication.** SMART Backend Services with an RS384 `private_key_jwt` assertion.
  - The key stays outside the repository.
  - The public JWK Set is hosted on GitHub Pages.
  - The token is held only in memory.
  - The FHIR and token hosts are constants.
  - Redirects are never followed.
  - Every resource must be bound to the requested patient.
- **Mapping.** Epic resources are normalised *to* the existing contract, and the unchanged `validate_resource` is the gate.
  - Each mapped resource records its original Epic reference, version, last update, content hash and retrieval time.
  - It also records every field that was transformed, substituted, unavailable or not carried.
  - Clinical times come only from source fields with the same meaning. Missing required clinical values cause a counted rejection, never an invented value.
  - Every resource is stored as version 1 of an immutable snapshot.
- **Storage.** Each import is a new snapshot under `var/epic_sandbox/`, with its own SQLite database built by the unchanged `build_runtime` and the readmission model disabled. The application's runtime database is refused.
- **Medications.** MedicationRequest is not imported until the medication-date fix (A1) is separately approved.

**Consequences.**

- The existing agent, retrieval, verifier and review run unchanged on Epic test patients, offline and deterministically. Citations trace back to the original Epic resource.
- The synthetic demo, public deployment and user interface are unaffected.
- Known gaps are recorded rather than hidden:
  - exact-case discharge-summary detection (fixed on `feature/beta` in M2.2-a);
  - medication absence while A1 is pending;
  - RTF-only notes;
  - `effectivePeriod` observations;
  - conditions without an ICD-10-CM code.
- Offline tests prove contract compatibility only. Live access is claimed only from recorded LIVE gate results.

**Amendment (2026-11-06).** Offline reviews of the LIVE snapshots led to owner-approved changes outside the adapter, in `src/agent/` (composer, tools) and `src/clinical/` (consistency, records):

- A note's labelled admission and discharge dates are compared with its linked encounter. A conflict is reported as a possible inconsistency; a missing encounter end is reported as incomplete documentation.
- When a record has no medication orders, a medication answer states the absence and the source's import coverage as uncited notes and quotes note entries as note text. A drug question is answered only from an entry that documents the asked event as done, with the drug's documented status first. No reason, current status or dose is inferred, and shown dates are labelled as note dates.

Consequences of the amendment:

- The statements above that the agent runs unchanged and that the synthetic demo is unaffected now describe the adapter alone. The code of retrieval, the verifier, the planner, the API, the user interface and deployment is still unchanged.
- The agent's execution revision changes, so checkpointed runs from before the change cannot be resumed after it.
- For one synthetic demo patient without medication orders (`synthetic_p003`), medication answers change once this work is merged and deployed (a separate decision). The other measured synthetic answers and the offline evaluation gate (41/42) are unchanged.
- Current limitations are listed in [EPIC_SANDBOX.md](../EPIC_SANDBOX.md#limitations-and-pending-decisions).

**M2.3 scope amendment (2026-11-06 America/New_York).** The original local-command-line-only boundaries above describe M1. M2-S1 already permits explicitly configured local snapshot serving. The approved M2.3 target must additionally reuse M1 OAuth, the read-only sandbox client, mapping/validation and provenance behind an owner-authenticated backend refresh service. It must add approved synthetic patient selection, refresh budgets, truthful freshness/coverage, atomic validated snapshot promotion/versioning and rollback before the evidence engine reads the new version. M1's direct directory/manifest writes do not provide that atomic publication boundary. The agent still never calls Epic while answering; Epic data stays deterministic-only with external tracing off. This is a documentation amendment, not implementation/live-call/deployment authority. [ADR 0009](0009-beta-channel-and-data-sources.md) and [ARCHITECTURE.md](../ARCHITECTURE.md#beta-m23-target-owner-only-live-sandbox-refresh) define the target without changing the historical M1 evidence.
