"""Create one new attended genuine owner store and binding after explicit owner approval."""

from __future__ import annotations

import argparse
import hashlib
import ntpath
import re
import sys
import time
from pathlib import Path
from typing import NoReturn


class ProvisionRejected(Exception):
    """Content-free refusal."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ProvisionRejected


def _provision(root: Path, principal: str, *, create_private_parent: bool, diagnostic_timing: bool = False) -> str:
    started = time.monotonic() if diagnostic_timing else 0.0

    def progress(stage: str) -> None:
        if diagnostic_timing:
            try:
                elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
                print(f"Owner diagnostic stage={stage} elapsed_ms={elapsed_ms}", flush=True)
            except Exception:
                pass  # Observation never changes the provisioner's checks or cleanup.

    def comparison(outcome: str, elapsed_ms: int) -> None:
        print(f"Owner diagnostic stage=parent_prompt_{outcome} elapsed_ms={elapsed_ms}", flush=True)

    progress("environment_begin")
    from scripts.run_owner_https_diagnostic import _lock_environment

    _lock_environment()
    if sys.platform != "win32" or sys.version_info < (3, 13):
        raise ProvisionRejected
    progress("environment_ready")

    progress("dependencies_begin")
    from api.beta_owner_console import confirm_owner_action
    from api.beta_owner_custody import Custody
    from api.beta_owner_genuine import provision_owner_store
    from api.beta_owner_operator import save_owner_binding
    from api.beta_webauthn_store import current_principal

    progress("dependencies_ready")
    progress("principal_check")
    if current_principal() != principal:
        raise ProvisionRejected
    if create_private_parent:
        parent = root.parent
        fingerprint = hashlib.sha256(f"NEW private owner parent\n{parent}\n{principal}".encode()).hexdigest()
        print("Create the approved empty private owner parent:", parent)
        progress("parent_confirmation_begin")
        if diagnostic_timing:
            confirmed = confirm_owner_action(principal, fingerprint, enabled=True, diagnostic=comparison)
        else:
            confirmed = confirm_owner_action(principal, fingerprint, enabled=True)
        if not confirmed:
            raise ProvisionRejected
        # The existing ancestor may allow reads. Hold its mutation checks through exclusive creation.
        progress("ancestor_custody_begin")
        with Custody(parent.parent, private_leaf=False) as ancestor:
            progress("ancestor_check")
            ancestor.check()
            progress("parent_create")
            parent.mkdir(mode=0o700)
            progress("parent_custody_begin")
            with Custody(parent) as custody:
                progress("parent_check")
                custody.check()

    print("Provision the approved new owner store and binding:", root)
    progress("store_provision_begin")
    store = provision_owner_store(root, principal, enabled=True, policy_accepted=True)
    try:
        progress("store_provision_ready")
        progress("binding_write_begin")
        digest = save_owner_binding(store, enabled=True)
        progress("binding_write_ready")
        return digest
    finally:
        try:
            progress("store_close_begin")
        finally:
            store.close()
        progress("store_close_ready")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--provision-owner-binding", action="store_true")
    parser.add_argument("--accept-owner-policy", action="store_true")
    parser.add_argument("--binding-root", type=Path)
    parser.add_argument("--approved-sid")
    parser.add_argument("--create-private-parent", action="store_true")
    parser.add_argument("--diagnostic-timing", action="store_true")
    try:
        args = parser.parse_args(argv)
        if not args.provision_owner_binding:
            print("Genuine owner provisioning is disabled. Separate identity and storage approval is required.")
            return 2
        root = args.binding_root
        if (
            not args.accept_owner_policy
            or root is None
            or not root.is_absolute()
            or len(root.drive) != 2
            or root.drive[1] != ":"
            or len(root.parts) < 3
            or any(part == ".." or ntpath.isreserved(part) for part in root.parts[1:])
            or not re.fullmatch(r"S-1-5-21-(?:[0-9]+-){3}[0-9]+", args.approved_sid or "")
        ):
            raise ProvisionRejected
        repo = str(Path(__file__).resolve().parents[1])
        if repo not in sys.path:
            sys.path.insert(0, repo)
        if args.diagnostic_timing:
            digest = _provision(
                root, args.approved_sid, create_private_parent=args.create_private_parent, diagnostic_timing=True
            )
        else:
            digest = _provision(root, args.approved_sid, create_private_parent=args.create_private_parent)
        print("Owner binding SHA256:", digest)
        return 0
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    except (Exception, KeyboardInterrupt):
        print("Owner provisioning refused or failed. Preserve any partial state for review.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
