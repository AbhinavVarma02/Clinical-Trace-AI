# Public JWK Set: Clinical Trace AI (Epic on FHIR sandbox, non-production)

`jwks.json` holds only the **public** RSA verification key(s) for the Clinical Trace AI app's Backend Services client on the Epic on FHIR public sandbox. Epic fetches it to verify the app's signed client assertions.

- The key is for non-production use only. The private key never leaves the developer's machine and is not in this repository.
- Each key id (`kid`) is the RFC 7638 thumbprint of its key. The algorithm is RS384.
- During key rotation the set holds at most two keys: the active key and the one being added or retired.
- This branch is served by GitHub Pages and holds nothing else. See `docs/EPIC_SANDBOX.md` on the project branches for the full procedure.
