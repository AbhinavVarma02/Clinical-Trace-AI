"""Command line for the Epic sandbox adapter.

    python -m src.interop.epic.cli keys generate [--out-dir DIR] [--bits 2048] [--no-passphrase]
    python -m src.interop.epic.cli jwks lint FILE
    python -m src.interop.epic.cli jwks merge FILE [FILE] --out OUT
    python -m src.interop.epic.cli jwks check [URL] [--key PEM | --kid KID]

Output is limited to key ids, file locations, check results and counts. Private key
material, tokens and clinical content are never printed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from src.interop.epic import keys, settings


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _passphrase(args: argparse.Namespace, *, confirm: bool) -> bytes | None:
    if getattr(args, "no_passphrase", False):
        return None
    value = os.environ.get(settings.PASSPHRASE_ENV)
    if value:
        return value.encode("utf-8")
    if not sys.stdin.isatty():
        raise keys.EpicKeyError(
            f"set {settings.PASSPHRASE_ENV} or pass --no-passphrase (no interactive terminal for a prompt)"
        )
    first = getpass.getpass("Key passphrase: ")
    if confirm and first != getpass.getpass("Repeat passphrase: "):
        raise keys.EpicKeyError("the passphrases do not match")
    if not first:
        raise keys.EpicKeyError("an empty passphrase is not allowed; use --no-passphrase instead")
    return first.encode("utf-8")


def _cmd_keys_generate(args: argparse.Namespace) -> int:
    out_dir = settings.require_outside_repo(args.out_dir)
    passphrase = _passphrase(args, confirm=True)
    private_key = keys.generate_private_key(args.bits)
    kid = keys.key_id(private_key)
    key_path = keys.write_private_key(private_key, out_dir / f"{kid}.pem", passphrase)
    jwks_path = keys.write_jwks(keys.build_jwks([keys.public_jwk(private_key)]), out_dir / f"{kid}.jwks.json")
    _emit(
        {
            "kid": kid,
            "bits": args.bits,
            "private_key_file": str(key_path),
            "private_key_encrypted": passphrase is not None,
            "public_jwks_file": str(jwks_path),
            "permission_advice": keys.key_file_permission_advice(key_path),
            "next_step": "publish only the public JWK Set file; never copy the .pem file anywhere",
        }
    )
    return 0


def _cmd_jwks_lint(args: argparse.Namespace) -> int:
    issues = keys.lint_jwks(keys.read_jwks(args.file))
    _emit({"file": str(Path(args.file).resolve()), "ok": not issues, "issues": issues})
    return 0 if not issues else 1


def _cmd_jwks_merge(args: argparse.Namespace) -> int:
    public: list[dict[str, Any]] = []
    for path in args.files:
        public.extend(keys.read_jwks(path).get("keys") or [])
    document = keys.build_jwks(public)
    target = keys.write_jwks(document, args.out)
    _emit({"out": str(target), "kids": [jwk["kid"] for jwk in document["keys"]]})
    return 0


def _cmd_jwks_check(args: argparse.Namespace) -> int:
    url = args.url or os.environ.get(settings.JKU_ENV)
    if not url:
        raise keys.EpicKeyError(f"pass the JWK Set URL or set {settings.JKU_ENV}")
    private_key = keys.load_private_key(args.key, _passphrase(args, confirm=False)) if args.key else None
    report = keys.check_published_jwks(url, private_key=private_key, expected_kid=args.kid)
    _emit(report.as_dict())
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.interop.epic.cli", description="Epic on FHIR sandbox adapter (read-only, local)."
    )
    groups = parser.add_subparsers(dest="group", required=True)

    key_group = groups.add_parser("keys", help="local signing keys").add_subparsers(dest="command", required=True)
    generate = key_group.add_parser("generate", help="create a non-production signing key outside the repository")
    generate.add_argument("--out-dir", default=str(settings.DEFAULT_KEY_DIR))
    generate.add_argument("--bits", type=int, default=2048, choices=keys.ALLOWED_KEY_SIZES)
    generate.add_argument("--no-passphrase", action="store_true", help="store the key unencrypted (not recommended)")
    generate.set_defaults(handler=_cmd_keys_generate)

    jwks_group = groups.add_parser("jwks", help="public JWK Set").add_subparsers(dest="command", required=True)
    lint = jwks_group.add_parser("lint", help="check a JWK Set file before publishing")
    lint.add_argument("file")
    lint.set_defaults(handler=_cmd_jwks_lint)
    merge = jwks_group.add_parser("merge", help="combine public JWK Sets (key rotation)")
    merge.add_argument("files", nargs="+")
    merge.add_argument("--out", required=True)
    merge.set_defaults(handler=_cmd_jwks_merge)
    check = jwks_group.add_parser("check", help="verify the published JWK Set (GET to the static host only)")
    check.add_argument("url", nargs="?")
    who = check.add_mutually_exclusive_group()
    who.add_argument("--key", help="local private key file; verifies the published key matches it")
    who.add_argument("--kid", help="expected key id when the private key is not available")
    check.add_argument("--no-passphrase", action="store_true", help="the local key file is not encrypted")
    check.set_defaults(handler=_cmd_jwks_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (keys.EpicKeyError, settings.LocalPathError) as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
