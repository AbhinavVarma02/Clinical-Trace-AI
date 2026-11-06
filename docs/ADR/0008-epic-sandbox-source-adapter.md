# ADR 0008: Epic sandbox records enter through an isolated, provenance-preserving adapter

**Status:** accepted for the internal milestone M1 (2026-11-05). Using the adapter from the application (M2) or publicly (M3) needs a separate decision.

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
  - exact-case discharge-summary detection;
  - medication absence while A1 is pending;
  - RTF-only notes;
  - `effectivePeriod` observations;
  - conditions without an ICD-10-CM code.
- Offline tests prove contract compatibility only. Live access is claimed only from recorded LIVE gate results.
