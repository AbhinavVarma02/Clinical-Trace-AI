# Dependency reproducibility and security checks

On 2026-10-17 the old successful CI run 39074327485 was inspected beyond its status: its advisory-only pip-audit step reported six known vulnerabilities in MLflow 3.14.0 and cryptography 48.0.1. The application does not run the MLflow AI Gateway or load user-uploaded MLflow models; nevertheless the dependency findings were remediated rather than ignored. The earlier green job was not a clean security audit.

Runtime roots are now exact pins, with constraints for the tested transitive set in `requirements-constraints.txt`. Development-only pytest, ruff, mypy and pip-audit are separated in `requirements-dev.txt`; constraints do not install those packages into the container. `pywin32` has a Windows marker. The snapshot was tested on Windows/Python 3.13; Linux/Python 3.12 portability passed in Actions 39077304115 at 6541d9b, including the actual container build/smoke test. Constraints pin package versions, not wheel hashes or base-image digests, and platform-only dependencies still need platform verification. This is not a complete software supply-chain attestation.

MLflow 3.17.1, cryptography 50.0.0 and checkpoint adapter 3.1.1 passed dependency consistency and existing MLflow/recovery tests. Auditing the original installed transitive snapshot also exposed 44 known findings across nine older packages; aiohttp, anyio, GitPython, langgraph-sdk, multidict, sqlparse, urllib3, pyasn1 and Werkzeug were updated to published fixes and the constraints regenerated. The final constrained audit reports **no known vulnerabilities among 144 resolved runtime packages**. Exact package versions/findings: [dependency_audit.json](../evaluation/results/dependency_audit.json). This is a time-bounded package-database check, not proof of absence of vulnerabilities.

```bash
pip install -r requirements.txt -r requirements-dev.txt
pip check
pip-audit -r requirements.txt --progress-spinner off
ruff check .
mypy api/v2_schemas.py src/persistence/metadata.py src/persistence/retention.py src/observability.py --follow-imports=silent --ignore-missing-imports --check-untyped-defs
```

CI now fails on dependency-audit findings or audit errors. Its Python type gate explicitly covers the four listed new modules, not every legacy Python file; formatting checks the new implementation/tests without a bulk legacy rewrite. Frontend type checking and both builds remain mandatory. Real-backend Playwright tests run on desktop/tablet/mobile with pinned Chromium. Secret scanning remains on tracked files only; private execution records, environment files, model/data artifacts and browser traces are excluded from publication/container build context.

Advisory context: [MLflow deserialization fix](https://github.com/advisories/GHSA-gqvg-gmmx-x4hm), [MLflow gateway advisory data](https://raw.githubusercontent.com/pypa/advisory-database/main/vulns/mlflow/PYSEC-2026-3865.yaml). No paid scanners or external inference providers were enabled.
