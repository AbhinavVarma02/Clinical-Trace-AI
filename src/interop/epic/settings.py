"""Fixed endpoints and local path guards for the Epic sandbox adapter.

The Epic hosts and paths are constants on purpose: configuration can select *which*
registered non-production client and key to use, but can never redirect requests
(or a bearer token) to a different host.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

SOURCE_SYSTEM = "epic-fhir-sandbox-r4"
EPIC_HOST = "fhir.epic.com"
FHIR_BASE_PATH = "/interconnect-fhir-oauth/api/FHIR/R4"
FHIR_BASE = f"https://{EPIC_HOST}{FHIR_BASE_PATH}"
TOKEN_URL = f"https://{EPIC_HOST}/interconnect-fhir-oauth/oauth2/token"

DEFAULT_CONFIG_DIR = Path.home() / ".clinical-trace" / "epic"
DEFAULT_KEY_DIR = DEFAULT_CONFIG_DIR / "keys"

ENABLE_ENV = "EPIC_SANDBOX_ENABLED"
PASSPHRASE_ENV = "EPIC_SANDBOX_KEY_PASSPHRASE"
JKU_ENV = "EPIC_SANDBOX_JKU"

USER_AGENT = "clinical-trace-epic-sandbox/1"


class LocalPathError(ValueError):
    """A path that must stay outside the repository points inside it."""


def is_inside_repo(path: str | Path) -> bool:
    """True when ``path`` resolves to the repository root or anywhere below it."""
    resolved = Path(path).expanduser().resolve()
    root = REPO_ROOT.resolve()
    return resolved == root or root in resolved.parents


def require_outside_repo(path: str | Path) -> Path:
    """Return the resolved path, refusing any location inside the repository tree."""
    if is_inside_repo(path):
        raise LocalPathError("refusing to use a location inside the repository for private key material")
    return Path(path).expanduser().resolve()
