"""Default-disabled genuine auth-only launcher. Real use needs separate owner approval.

No provisioning/reset, browser launch, certificate/trust changes, Epic request or LIVE activation.
The owner supplies a previously approved private binding root/digest and reviewed genuine UI assets.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NoReturn


RUN_LIMIT_S = 600  # the approved 10-minute attended bound, enforced by the listener itself
RUN_LIMIT_EXIT = 3  # the bound (not the owner) ended the listener; same code as the operator's hard stop


class OwnerLaunchRejected(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise OwnerLaunchRejected


def _serve(args: argparse.Namespace) -> bool:
    # Reuse N2's environment exclusion BEFORE any app/credential/native-principal dependency imports.
    from scripts.run_owner_https_diagnostic import _lock_environment

    _lock_environment()
    import asyncio
    from api.beta_owner_genuine import create_genuine_owner_app, prepare_genuine_owner_https
    from api.beta_owner_operator import open_owner_binding, serve_owner_auth

    store = open_owner_binding(args.binding_root, args.binding_sha256, enabled=True, policy_accepted=True)
    try:
        app = create_genuine_owner_app(store, enabled=True, policy_accepted=True, browser_assets_root=args.assets)
        config = prepare_genuine_owner_https(app, args.certificate, args.private_key, enabled=True)
        return asyncio.run(
            serve_owner_auth(
                app,
                config,
                enabled=True,
                enrollment_approved=args.allow_enrollment,
                revocation_approved=args.allow_revocation,
                stop_after_s=RUN_LIMIT_S,
            )
        )
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--serve-owner-auth", action="store_true")
    parser.add_argument(
        "--accept-owner-policy",
        action="store_true",
        help="separately accepted attended-platform-UV and no-reset/no-restore policy",
    )
    parser.add_argument("--binding-root", type=Path)
    parser.add_argument("--binding-sha256")
    parser.add_argument("--certificate", type=Path)
    parser.add_argument("--private-key", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--allow-enrollment", action="store_true")
    parser.add_argument("--allow-revocation", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.serve_owner_auth is not True:
            print("Genuine owner auth is disabled. Separate trust, listener and identity approval is required.")
            return 2
        paths = (args.binding_root, args.certificate, args.private_key, args.assets)
        if (
            args.accept_owner_policy is not True
            or any(path is None or not path.is_absolute() for path in paths)
            or not isinstance(args.binding_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", args.binding_sha256)
        ):
            raise OwnerLaunchRejected
        # Direct script execution adds only this repository, never a request-selected Python module.
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)
        return RUN_LIMIT_EXIT if _serve(args) is True else 0
    except KeyboardInterrupt:
        return 0
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    except Exception:
        print("Owner auth refused or failed; no fallback. Review is required.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
