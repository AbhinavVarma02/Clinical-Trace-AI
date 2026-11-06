"""Command line for the Epic sandbox adapter.

    python -m src.interop.epic.cli keys generate [--out-dir DIR] [--bits 2048] [--no-passphrase]
    python -m src.interop.epic.cli jwks lint FILE
    python -m src.interop.epic.cli jwks merge FILE [FILE] --out OUT
    python -m src.interop.epic.cli jwks check [URL] [--key PEM | --kid KID]

    # LIVE: contacts the Epic sandbox; requires EPIC_SANDBOX_ENABLED=1 and owner approval
    python -m src.interop.epic.cli live token-check [--preflight]
    python -m src.interop.epic.cli live smoke --patient EPIC_TEST_PATIENT_ID [--all-types]
    python -m src.interop.epic.cli live import --patient EPIC_TEST_PATIENT_ID [--max-notes 10]

    # Offline: isolated snapshots under var/epic_sandbox/
    python -m src.interop.epic.cli workspace import-fixture FILE --key NAME
    python -m src.interop.epic.cli workspace list | show SNAPSHOT | verify SNAPSHOT
    python -m src.interop.epic.cli workspace review SNAPSHOT --question TEXT [--show-answer]

Output is limited to key ids, file locations, check results, statuses and counts.
Private key material, tokens and clinical content are never printed (``--show-answer``
prints the agent's answer to the local terminal only, on request).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from src.clinical.fhir_client import PatientScopeViolation
from src.interop.epic import keys, settings, workspace
from src.interop.epic.auth import EpicAuthError, EpicTokenProvider
from src.interop.epic.client import BinaryContent, EpicFHIRError, EpicSandboxClient


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


# --------------------------------------------------------------------------- LIVE (Epic sandbox)
def _live_session(args: argparse.Namespace) -> tuple[settings.EpicConfig, Any, EpicTokenProvider]:
    settings.require_enabled()
    config = settings.load_config()
    private_key = keys.load_private_key(config.private_key_path, _passphrase(args, confirm=False))
    return config, private_key, EpicTokenProvider(config.client_id, private_key, jku=config.jku)


def _save_report(name: str, payload: dict[str, Any]) -> str:
    directory = settings.workspace_root() / "reports"
    workspace.require_workspace_path(directory / "report.json")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{workspace.new_snapshot_id()}-{name}.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(target)


def _cmd_live_token_check(args: argparse.Namespace) -> int:
    config, private_key, tokens = _live_session(args)
    try:
        payload: dict[str, Any] = {"label": "LIVE", "kid": tokens.kid, "client": config.client_hint}
        jku = args.jwks_url or config.jku
        if args.preflight:
            if not jku:
                raise settings.ConfigError(f"--preflight needs --jwks-url or {settings.JKU_ENV}")
            report = keys.check_published_jwks(jku, private_key=private_key)
            payload["jwks_preflight"] = {
                "ok": report.ok,
                "issues": report.issues,
                "cache_control": report.cache_control,
            }
            if not report.ok:
                payload["ok"] = False
                _emit(payload)
                return 1
        token = tokens.token()
        payload.update(
            {
                "ok": True,
                "checked_at": workspace.utc_now(),
                "expires_in_s": round(token.expires_at - time.time()),
                "granted_scopes": list(token.scopes),
                "telemetry": tokens.telemetry.describe(),
            }
        )
        payload["report_file"] = _save_report("token-check", payload)
        _emit(payload)
        return 0
    finally:
        tokens.close()


def _cmd_live_smoke(args: argparse.Namespace) -> int:
    from src.interop.epic import importer

    _config, _key, tokens = _live_session(args)
    try:
        with EpicSandboxClient(tokens) as client:
            result = importer.smoke(client, args.patient, include_all_types=args.all_types)
        payload = {
            "label": "LIVE",
            "checked_at": workspace.utc_now(),
            **result,
            "telemetry": tokens.telemetry.describe(),
        }
        payload["report_file"] = _save_report("smoke", payload)
        _emit(payload)
        return 0 if result["all_ok"] else 1
    finally:
        tokens.close()


def _cmd_live_import(args: argparse.Namespace) -> int:
    from src.interop.epic import importer

    _config, _key, tokens = _live_session(args)
    try:
        retrieved_at = workspace.utc_now()
        with EpicSandboxClient(tokens) as client:
            fetched = importer.fetch_patient(client, args.patient, max_notes=args.max_notes)
        snapshot = workspace.write_snapshot(
            args.patient,
            fetched.raw_resources,
            fetched.binaries,
            retrieved_at=retrieved_at,
            mode="LIVE",
            fetch_summary=fetched.summary,
        )
        _emit(_snapshot_view(snapshot))
        return 0
    finally:
        tokens.close()


# --------------------------------------------------------------------------- offline workspace
def _snapshot_view(snapshot: workspace.Snapshot) -> dict[str, Any]:
    manifest = snapshot.manifest
    return {
        "snapshot_id": manifest["snapshot_id"],
        "mode": manifest["mode"],
        "path": str(snapshot.path),
        "patients": [p["internal_id"] for p in manifest["patients"]],
        "resource_counts": manifest["resource_counts"],
        "mapping": manifest["mapping"],
        "fetch_complete": (manifest.get("fetch") or {}).get("complete"),
        "notes": manifest["notes"],
    }


def _cmd_workspace_import_fixture(args: argparse.Namespace) -> int:
    document = json.loads(Path(args.file).read_text(encoding="utf-8"))
    patient = document["patients"][args.key]
    binaries = {
        ref: BinaryContent(reference=ref, content_type=item["contentType"], data=item["text"].encode())
        for ref, item in (patient.get("binaries") or {}).items()
    }
    snapshot = workspace.write_snapshot(
        patient["patient_id"],
        patient["resources"],
        binaries,
        retrieved_at=workspace.utc_now(),
        mode="MOCK",
        fetch_summary={"source": "hand-authored fixture", "complete": True},
    )
    _emit(_snapshot_view(snapshot))
    return 0


def _cmd_workspace_list(_args: argparse.Namespace) -> int:
    root = settings.workspace_root()
    items = []
    for manifest_path in sorted(root.glob("*/manifest.json")) if root.exists() else []:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        items.append(
            {
                "snapshot_id": manifest.get("snapshot_id"),
                "mode": manifest.get("mode"),
                "created_at": manifest.get("created_at"),
                "resources": manifest.get("resource_counts"),
            }
        )
    _emit({"snapshots": items})
    return 0


def _cmd_workspace_show(args: argparse.Namespace) -> int:
    _emit(_snapshot_view(workspace.open_snapshot(args.snapshot)))
    return 0


def _cmd_workspace_verify(args: argparse.Namespace) -> int:
    result = workspace.verify_snapshot(args.snapshot)
    _emit(result)
    return 0 if result["ok"] else 1


def _cmd_workspace_review(args: argparse.Namespace) -> int:
    snapshot = workspace.open_snapshot(args.snapshot)
    patient_id = args.patient or snapshot.patient_ids[0]
    runtime = workspace.load_runtime(snapshot.path)
    try:
        result = runtime.agent.run(workspace.local_access(snapshot), patient_id, args.question)
        payload: dict[str, Any] = {
            "label": snapshot.manifest["mode"],
            "snapshot_id": snapshot.manifest["snapshot_id"],
            "patient_id": patient_id,
            **workspace.review_summary(result),
            "citation_provenance": workspace.citation_provenance(runtime, result),
            "scope_notes": snapshot.manifest["notes"],
        }
        if args.show_answer:
            payload["answer"] = result["answer"]
        _emit(payload)
        return 0 if result["outcome"] in ("answered", "abstained", "refused") else 1
    finally:
        runtime.close()


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

    live = groups.add_parser("live", help="LIVE Epic sandbox calls (EPIC_SANDBOX_ENABLED=1 required)")
    live_group = live.add_subparsers(dest="command", required=True)
    token_check = live_group.add_parser("token-check", help="obtain one token; print granted scopes only")
    token_check.add_argument("--preflight", action="store_true", help="verify the published JWK Set first")
    token_check.add_argument("--jwks-url", help=f"JWK Set URL (default: {settings.JKU_ENV})")
    token_check.set_defaults(handler=_cmd_live_token_check)
    smoke = live_group.add_parser("smoke", help="Patient.Read plus one search per enabled type/category")
    smoke.add_argument("--patient", required=True, help="an Epic sandbox test patient FHIR id")
    smoke.add_argument("--all-types", action="store_true", help="include MedicationRequest, notes and reports")
    smoke.set_defaults(handler=_cmd_live_smoke)
    live_import = live_group.add_parser("import", help="import one test patient into a new isolated snapshot")
    live_import.add_argument("--patient", required=True, help="an Epic sandbox test patient FHIR id")
    live_import.add_argument("--max-notes", type=int, default=10)
    live_import.set_defaults(handler=_cmd_live_import)
    for sub in (token_check, smoke, live_import):
        sub.add_argument("--no-passphrase", action="store_true", help="the local key file is not encrypted")

    ws = groups.add_parser("workspace", help="offline isolated snapshots").add_subparsers(dest="command", required=True)
    fixture = ws.add_parser("import-fixture", help="build a MOCK snapshot from an Epic-shaped fixture file")
    fixture.add_argument("file")
    fixture.add_argument("--key", required=True, help="patient key inside the fixture file")
    fixture.set_defaults(handler=_cmd_workspace_import_fixture)
    ws.add_parser("list", help="list snapshots").set_defaults(handler=_cmd_workspace_list)
    for name, handler, text in (
        ("show", _cmd_workspace_show, "summarise a snapshot"),
        ("verify", _cmd_workspace_verify, "re-map raw data and check provenance"),
    ):
        command = ws.add_parser(name, help=text)
        command.add_argument("snapshot")
        command.set_defaults(handler=handler)
    review = ws.add_parser("review", help="run the existing deterministic agent on a snapshot patient")
    review.add_argument("snapshot")
    review.add_argument("--question", required=True)
    review.add_argument("--patient", help="internal patient id (default: the snapshot's patient)")
    review.add_argument("--show-answer", action="store_true", help="also print the answer to this terminal")
    review.set_defaults(handler=_cmd_workspace_review)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        keys.EpicKeyError,
        settings.LocalPathError,
        settings.ConfigError,
        settings.SandboxDisabled,
        workspace.WorkspaceError,
        ValueError,
    ) as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2
    except (EpicAuthError, EpicFHIRError, PatientScopeViolation) as exc:
        _emit({"ok": False, "error": str(exc), "category": getattr(exc, "category", "patient_scope")})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
