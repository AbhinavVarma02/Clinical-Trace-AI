"""Run one separately approved, attended owner Epic phase on the fixed local HTTPS origin."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

_PHASES = ("acquire", "review", "promote", "serve")
_WINDOW_SECONDS = 600


class _Refused(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _Refused


def _validate(args: argparse.Namespace) -> None:
    if args.accept_owner_policy is not True:
        raise _Refused
    paths = (args.binding_root, args.certificate, args.private_key, args.assets, args.private_root)
    if (
        any(path is None or not path.is_absolute() for path in paths)
        or not re.fullmatch(r"[a-f0-9]{64}", args.binding_sha256 or "")
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", args.alias or "")
        or not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", args.patient_id or "")
        or args.patient_id in {".", ".."}
    ):
        raise _Refused
    if any(
        root == args.private_root or args.private_root in root.parents or root in args.private_root.parents
        for root in (*paths[:-1], *([args.epic_key] if args.epic_key else []))
    ):
        raise _Refused
    if args.phase == "serve":
        if args.approval_id is not None:
            raise _Refused
    elif not re.fullmatch(r"[A-Za-z0-9_-]{43}", args.approval_id or ""):
        raise _Refused
    credential_fields = (args.client_id, args.epic_key, args.epic_key_id, args.jku)
    if args.phase == "acquire":
        if (
            not re.fullmatch(r"[A-Za-z0-9-]{8,128}", args.client_id or "")
            or args.epic_key is None
            or not args.epic_key.is_absolute()
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", args.epic_key_id or "")
        ):
            raise _Refused
        if args.jku is not None:
            jku = urlsplit(args.jku)
            if (
                jku.scheme != "https"
                or not jku.hostname
                or jku.username
                or jku.password
                or jku.query
                or jku.fragment
                or any(char.isspace() for char in args.jku)
            ):
                raise _Refused
    elif any(value is not None for value in credential_fields):
        raise _Refused
    evidence_fields = (args.attempt_id, args.generation_id, args.manifest_sha256, args.bundle_sha256)
    if args.phase in {"promote", "serve"}:
        for value, pattern in zip(
            evidence_fields, (r"[A-Za-z0-9_-]{16,64}", r"[a-f0-9]{32}", r"[a-f0-9]{64}", r"[a-f0-9]{64}"), strict=True
        ):
            if not re.fullmatch(pattern, value or ""):
                raise _Refused
    elif any(value is not None for value in evidence_fields):
        raise _Refused


def _unlock(args, store, allowlist):
    import getpass
    import warnings

    from api.beta_owner_console import _local_console
    from api.beta_owner_custody import Custody
    from src.interop.epic import keys, settings
    from src.interop.epic.beta_live_acquisition import LiveRefresh

    if not _local_console():
        raise _Refused
    with Custody(args.epic_key.parent) as custody:
        custody.file(args.epic_key)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            passphrase = getpass.getpass("Epic encrypted key passphrase: ")
        if not passphrase or not _local_console() or store.load().credential is None:
            raise _Refused
        key = keys.load_private_key(args.epic_key, passphrase.encode())
        del passphrase
        custody.check()
    if keys.public_jwk(key)["kid"] != args.epic_key_id:
        raise _Refused
    return LiveRefresh(key, settings.EpicConfig(args.client_id, args.epic_key, args.jku), allowlist)


async def _listen(config) -> None:
    import uvicorn

    config.timeout_graceful_shutdown = 5
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait({task}, timeout=_WINDOW_SECONDS)
    finally:
        server.should_exit = True
        await task
    if not server.started:
        raise _Refused


def _serve(args: argparse.Namespace) -> None:
    from scripts.run_owner_https_diagnostic import _lock_environment

    _lock_environment()
    import time

    from api.beta_browser import BrowserAssets
    from api.beta_live_record import LiveEvidenceBinding
    from api.beta_owner_console import _local_console
    from api.beta_owner_coordinator import AcquisitionPlan, GenuineOwnerCoordinatorApp
    from api.beta_owner_custody import Custody
    from api.beta_owner_genuine import create_genuine_owner_app
    from api.beta_owner_https import prepare_bootstrap
    from api.beta_owner_operator import open_owner_binding
    from src.interop.epic import beta_live_staging as files
    from src.interop.epic.beta_catalog import Catalog
    from src.interop.epic.beta_live_generations import LiveGenerations
    from src.interop.epic.settings import ENABLE_ENV

    if not _local_console():
        raise _Refused
    allowlist = files.LiveAllowlist({args.alias: args.patient_id})
    store = open_owner_binding(args.binding_root, args.binding_sha256, enabled=True, policy_accepted=True)
    try:
        owner = store.load()
        if owner.credential is None or owner.revoked:
            raise _Refused
        config = prepare_bootstrap(store.policy, args.certificate, args.private_key, enabled=True)
        assets = BrowserAssets.load(args.assets) if args.phase != "serve" else None
        with Custody(args.private_root) as custody:
            staging_root = args.private_root / "staging"
            generation_root = args.private_root / "generations"
            catalog_path = args.private_root / "catalog.sqlite"
            acquire = args.phase == "acquire"
            files.verify_private_path(args.private_root)
            if acquire and any(args.private_root.iterdir()):
                raise _Refused  # No rerun, recovery, or reuse of an existing acquisition store.
            for suffix in ("-journal", "-wal", "-shm"):
                if os.path.lexists(catalog_path.with_name(catalog_path.name + suffix)):
                    raise _Refused
            if not acquire:
                for folder in (staging_root, generation_root):
                    if not folder.is_dir() or files._is_link(folder.lstat()):
                        raise _Refused
                    files.verify_private_path(folder)
            live = _unlock(args, store, allowlist) if acquire else None
            readonly = args.phase in {"review", "serve"}
            custody.file(catalog_path, create=acquire, writable=not readonly)
            files.verify_private_path(catalog_path)
            catalog = Catalog(catalog_path, time.time, initialize=acquire, readonly=readonly)
            try:
                staging = files.LiveStaging(catalog, staging_root, initialize=acquire)
                generations = LiveGenerations(
                    catalog,
                    generation_root,
                    allowlist,
                    staging,
                    real_promotion=args.phase in {"promote", "serve"},
                    initialize=acquire,
                )
                evidence = (
                    LiveEvidenceBinding(args.attempt_id, args.generation_id, args.manifest_sha256, args.bundle_sha256)
                    if args.phase in {"promote", "serve"}
                    else None
                )
                if args.phase == "serve":
                    assert evidence is not None

                    def approved(manifest, bundle, row):
                        if manifest["synthetic_transport"] is not False:
                            raise _Refused
                        return evidence.require(manifest, row)

                    generations.project_active(args.alias, lambda: not store.load().revoked, approved)
                    app = create_genuine_owner_app(
                        store,
                        enabled=True,
                        policy_accepted=True,
                        reads=generations,
                        live_reads_approved=True,
                        approved_evidence=evidence,
                        browser_assets_root=args.assets,
                    )
                else:
                    coordinator = GenuineOwnerCoordinatorApp(
                        store,
                        AcquisitionPlan(args.approval_id, args.alias, args.patient_id, args.private_root),
                        live,
                        generations,
                        enabled=True,
                        policy_accepted=True,
                        acquisition_approved=acquire,
                        promotion_approved=args.phase == "promote",
                        approved_candidate=evidence,
                        assets=assets,
                    )
                    if not acquire:
                        coordinator._admit(args.phase)
                        binding, _, _ = coordinator._candidate()
                        if evidence is not None and binding != asdict(evidence):
                            raise _Refused
                    app = coordinator
                custody.check()
                store.load()
                if not _local_console():
                    raise _Refused
                config.app = config.loaded_app = app
                if acquire:
                    os.environ[ENABLE_ENV] = "1"  # process-local enable, after exact phase and attended unlock
                print(f"Owner Epic {args.phase} at https://owner.clinical-trace.localhost:8443. Stop with Ctrl+C.")
                asyncio.run(_listen(config))
            finally:
                os.environ.pop(ENABLE_ENV, None)
                catalog.close()
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--phase", choices=_PHASES)
    parser.add_argument("--accept-owner-policy", action="store_true")
    for name in ("binding-root", "certificate", "private-key", "assets", "private-root"):
        parser.add_argument("--" + name, type=Path)
    for name in (
        "binding-sha256",
        "alias",
        "patient-id",
        "approval-id",
        "client-id",
        "epic-key-id",
        "jku",
        "attempt-id",
        "generation-id",
        "manifest-sha256",
        "bundle-sha256",
    ):
        parser.add_argument("--" + name)
    parser.add_argument("--epic-key", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.phase is None:
            print("Owner Epic is disabled. Separate approval is required for each phase.")
            return 2
        _validate(args)
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)
        _serve(args)
        return 0
    except KeyboardInterrupt:
        return 0
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    except Exception:
        print("Owner Epic phase refused or failed. Review is required.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
