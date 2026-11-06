"""Fixed endpoints and local path guards for the Epic sandbox adapter.

The Epic hosts and paths are constants on purpose: configuration can select *which*
registered non-production client and key to use, but can never redirect requests
(or a bearer token) to a different host.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

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


# --------------------------------------------------------------------------- local configuration
CLIENT_ID_ENV = "EPIC_SANDBOX_CLIENT_ID"
KEY_PATH_ENV = "EPIC_SANDBOX_PRIVATE_KEY_PATH"
ENV_FILE_ENV = "EPIC_SANDBOX_ENV_FILE"
DEFAULT_ENV_FILE = DEFAULT_CONFIG_DIR / "epic.env"
_CLIENT_ID = re.compile(r"^[A-Za-z0-9-]{8,128}$")


class ConfigError(ValueError):
    """Local Epic sandbox configuration is missing or unsafe. Messages hold no secret values."""


class SandboxDisabled(RuntimeError):
    """Live Epic sandbox calls were requested without the explicit opt-in flag."""


@dataclass(frozen=True)
class EpicConfig:
    """The owner's existing, registered non-production Epic app and its local signing key."""

    client_id: str = field(repr=False)
    private_key_path: Path
    jku: str | None = None

    @property
    def client_hint(self) -> str:
        """Last four characters only, for sanitised reports."""
        return f"...{self.client_id[-4:]}"


def workspace_root() -> Path:
    return REPO_ROOT / "var" / "epic_sandbox"


def require_enabled(environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    if env.get(ENABLE_ENV) != "1":
        raise SandboxDisabled(f"live Epic sandbox calls are disabled; set {ENABLE_ENV}=1 for this session")


def load_config(environ: Mapping[str, str] | None = None) -> EpicConfig:
    """Read the local config file (outside the repository) overlaid with ``EPIC_SANDBOX_*`` variables."""
    env = os.environ if environ is None else environ
    values: dict[str, str] = {}
    env_file = Path(env.get(ENV_FILE_ENV) or DEFAULT_ENV_FILE).expanduser()
    if env_file.exists():
        require_outside_repo(env_file)
        file_values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
        if PASSPHRASE_ENV in file_values:
            raise ConfigError(f"keep {PASSPHRASE_ENV} out of the config file; set it only in the session")
        values.update(file_values)
    values.update({k: v for k, v in env.items() if k.startswith("EPIC_SANDBOX_")})
    client_id = (values.get(CLIENT_ID_ENV) or "").strip()
    if not _CLIENT_ID.fullmatch(client_id):
        raise ConfigError(f"{CLIENT_ID_ENV} must be the existing non-production client ID of the registered app")
    key_path = (values.get(KEY_PATH_ENV) or "").strip()
    if not key_path:
        raise ConfigError(f"{KEY_PATH_ENV} must point to the local signing key (outside the repository)")
    jku = (values.get(JKU_ENV) or "").strip() or None
    return EpicConfig(client_id=client_id, private_key_path=require_outside_repo(key_path), jku=jku)
