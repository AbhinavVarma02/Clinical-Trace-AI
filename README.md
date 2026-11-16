# Public JWK Set: Clinical Trace AI (Epic on FHIR sandbox, non-production)

`jwks.json` holds only the **public** RSA verification key(s) for the Clinical Trace AI app's Backend Services client on the Epic on FHIR public sandbox. Epic fetches it to verify the app's signed client assertions.

- The keys are for non-production use only. No private key is in this repository.
- The set holds two independent keys, each with its own private half:
  - the developer workstation key (`WHm96…SnyI`), whose private half never leaves the developer's machine;
  - the owner-only hosted Beta key (`wiOez…FsR04`), whose private half exists only in the hosted Beta Worker's secret store.
- Removing a key's entry here revokes it. Each key id (`kid`) is the RFC 7638 thumbprint of its key. The algorithm is RS384.
- During a rotation of either key, the set may briefly hold one more key: the one being added or retired.
- This branch is served by GitHub Pages and holds nothing else. See `docs/EPIC_SANDBOX.md` on the project branches for the full procedure.
