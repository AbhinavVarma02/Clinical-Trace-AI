"""Read-only adapter for the Epic on FHIR public R4 sandbox (synthetic test patients only).

Scope and boundaries:

* Backend Services OAuth (``private_key_jwt``) for the owner's existing, registered
  non-production Epic application. The private signing key stays on the local
  machine, outside this repository. Only the public JWK Set is published.
* Read-only requests, pinned to the Epic sandbox host. No production Epic
  connectivity and no write operations.
* Imported records are normalised to the existing Clinical-Trace FHIR contract and
  stored in an isolated workspace under ``var/epic_sandbox/``. They are never mixed
  into the default synthetic runtime database and are not exposed through the API
  or the user interface.
"""
