"""Owner-run: create the hosted-only Epic signing key and set every hosted LIVE Beta secret in Cloudflare.

Run once on the owner's workstation, after the first deploy of the live Worker and after Cloudflare Access is enabled
on it. The new key pair exists only in memory here: its private half goes straight into the Worker's secret store
through ``wrangler secret bulk`` (stdin, never a file or the terminal), and its public half is merged into a new JWK Set
file for the existing ``epic-jwks`` publication. The local key that was used on 2026-11-13 is not read or changed.

Reads, without printing: the registered client ID and JWK Set URL from the existing local Epic config
(``~/.clinical-trace/epic/epic.env``). Asks for: the Access team domain, the Access application audience (AUD) tag,
the owner's Access email and the approved sandbox patient's FHIR ID. Prints only the new key id, the JWK Set file path
and the names of the secrets that were set.

    python scripts/provision_beta_live_secrets.py            # validate inputs, build the JWK Set, upload secrets
    python scripts/provision_beta_live_secrets.py --dry-run  # everything except the upload (the key is discarded)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CLINICAL_TRACE_SKIP_DOTENV", "1")

import httpx  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

from api.beta_hosted.access import AccessPolicy  # noqa: E402
from api.beta_hosted.hosted_keys import load_pem_key  # noqa: E402
from src.interop.epic import keys, settings  # noqa: E402
from src.interop.epic.client import valid_fhir_id  # noqa: E402

CONFIG = "beta-live/wrangler.jsonc"
EDGE = ROOT / "deploy" / "cloudflare"
DEFAULT_JWKS_OUT = settings.DEFAULT_CONFIG_DIR / "jwks.with-hosted-beta-live.json"
SECRET_NAMES = (
    "BETA_ACCESS_TEAM_DOMAIN", "BETA_ACCESS_AUD", "BETA_OWNER_EMAIL", "BETA_PATIENT_ID",
    "EPIC_CLIENT_ID", "EPIC_KEY_ID", "EPIC_PRIVATE_KEY_PEM", "EPIC_JKU",
)


def published_jwks(url: str, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=10, transport=transport, trust_env=False, follow_redirects=False) as client:
        response = client.get(url, headers={"Accept": "application/json"})
    if response.status_code != 200 or len(response.content) > keys.MAX_JWKS_BYTES:
        raise SystemExit(f"The published JWK Set could not be read (HTTP {response.status_code}).")
    document = response.json()
    issues = keys.lint_jwks(document)
    if issues:
        raise SystemExit("The published JWK Set is not valid: " + "; ".join(issues))
    return document


def build(inputs: dict[str, str], config: settings.EpicConfig, current: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any], str]:
    """Validate everything, then create the key. Returns (secrets, merged JWK Set, kid)."""
    AccessPolicy(inputs["team"], inputs["aud"], inputs["owner"])
    valid_fhir_id(inputs["patient"])
    if config.jku is None:
        raise SystemExit("The local Epic config has no JWK Set URL (EPIC_SANDBOX_JKU); the hosted key must be published there.")
    key = keys.generate_private_key(2048)
    kid = keys.key_id(key)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    load_pem_key(pem, kid)  # the backend's own loader must accept exactly this value
    published = [jwk for jwk in current["keys"] if jwk.get("kid") != kid]
    merged = keys.build_jwks([*published, keys.public_jwk(key)])
    secrets = {
        "BETA_ACCESS_TEAM_DOMAIN": inputs["team"],
        "BETA_ACCESS_AUD": inputs["aud"],
        "BETA_OWNER_EMAIL": inputs["owner"],
        "BETA_PATIENT_ID": inputs["patient"],
        "EPIC_CLIENT_ID": config.client_id,
        "EPIC_KEY_ID": kid,
        "EPIC_PRIVATE_KEY_PEM": pem.decode("ascii"),
        "EPIC_JKU": config.jku,
    }
    del key, pem
    return secrets, merged, kid


def wrangler_bulk_command() -> list[str]:
    return ["npx.cmd" if os.name == "nt" else "npx", "wrangler", "secret", "bulk", "--config", CONFIG]


def upload(
    secrets: dict[str, str],
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    command: list[str] | None = None,
) -> None:
    result = run(
        command or wrangler_bulk_command(),
        input=json.dumps(secrets),
        cwd=EDGE,
        text=True,
        # Wrangler writes UTF-8 (emoji, box drawing). Without an explicit encoding Windows decodes it with the ANSI
        # code page (cp1252) in subprocess's reader thread and raises UnicodeDecodeError there; the output is only
        # discarded, so decode it as UTF-8 and never fail on it.
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        # Wrangler's own output never contains secret values, but keep it out of the terminal anyway.
        raise SystemExit(f"wrangler secret bulk failed (exit {result.returncode}). Nothing was published to the JWK Set.")


def ask(prompt: str, reader: Callable[[str], str] = input) -> str:
    value = reader(prompt).strip()
    if not value:
        raise SystemExit("A value is required.")
    return value


def main(argv: list[str] | None = None, *, reader: Callable[[str], str] = input,
         run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
         transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="validate and build the JWK Set; upload nothing")
    parser.add_argument("--jwks-out", type=Path, default=DEFAULT_JWKS_OUT, help="where to write the merged public JWK Set")
    args = parser.parse_args(argv)
    config = settings.load_config()
    current = published_jwks(config.jku, transport) if config.jku else {"keys": []}
    inputs = {
        "team": ask("Cloudflare Access team domain (https://<team>.cloudflareaccess.com): ", reader),
        "aud": ask("Access application audience (AUD) tag: ", reader),
        "owner": ask("Owner email allowed by the Access policy: ", reader),
        "patient": ask("Approved Epic sandbox patient FHIR ID: ", reader),
    }
    secrets, merged, kid = build(inputs, config, current)
    out = settings.require_outside_repo(args.jwks_out)
    if args.dry_run:
        print(f"Dry run: inputs valid; a key would be created (this one, {kid}, is discarded). Nothing uploaded or written.")
        return 0
    upload(secrets, run)
    keys.write_jwks(merged, out)
    print(f"Set {len(secrets)} Worker secrets: {', '.join(sorted(secrets))}")
    print(f"Hosted Epic key id: {kid}")
    print(f"Merged public JWK Set (publish to epic-jwks): {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
