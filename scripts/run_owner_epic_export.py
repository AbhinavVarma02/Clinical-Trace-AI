"""Write one owner-approved publication copy of the promoted Epic generation, offline. Disabled by default.

The copy comes from the accepted read path (custody, ``LiveGenerations`` integrity and the pinned attempt,
generation, manifest and bundle) and from ``api.beta_publication``. No Epic request, owner proof, listener or TLS
is involved. The single output file is created in an existing private directory outside the repository. Whether
the copy may then be published is a separate owner decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import NoReturn

_REPO = Path(__file__).resolve().parents[1]


class _Refused(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _Refused


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _in_work_tree(path: Path) -> bool:
    return any((folder / ".git").exists() for folder in (path, *path.parents))


def _validate(args: argparse.Namespace) -> None:
    if args.accept_owner_policy is not True:
        raise _Refused
    given = (args.binding_root, args.private_root, args.out)
    if any(path is None or not path.is_absolute() or ".." in path.parts for path in given):
        raise _Refused
    # Compare real locations: junctions, 8.3 names and symbolic links resolve before any containment check.
    roots = tuple(path.resolve() for path in (args.binding_root, args.private_root))
    if _inside(roots[0], roots[1]) or _inside(roots[1], roots[0]):
        raise _Refused
    try:
        parent = args.out.parent.resolve(strict=True)
    except OSError:
        raise _Refused from None
    out = parent / args.out.name
    if (
        not parent.is_dir()
        or not re.fullmatch(r"[a-f0-9]{64}", args.binding_sha256 or "")
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", args.alias or "")
        or not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", args.patient_id or "")
        or args.patient_id in {".", ".."}
    ):
        raise _Refused
    for value, pattern in (
        (args.attempt_id, r"[A-Za-z0-9_-]{16,64}"),
        (args.generation_id, r"[a-f0-9]{32}"),
        (args.manifest_sha256, r"[a-f0-9]{64}"),
        (args.bundle_sha256, r"[a-f0-9]{64}"),
    ):
        if not re.fullmatch(pattern, value or ""):
            raise _Refused
    repo = _REPO.resolve()
    if (
        out.suffix != ".json"
        or os.path.lexists(out)
        or any(_inside(out, root) or _inside(root, parent) for root in roots)
        or _inside(out, repo)
        or _inside(repo, parent)
        or _in_work_tree(parent)
    ):
        raise _Refused
    args.out = out  # write only to the resolved location that was checked


def _read_view(args: argparse.Namespace) -> dict:
    # Same process gates as the serve phase: a locked environment (no .env, tracing or provider settings) before
    # any application module loads, and an attended local owner console.
    from scripts.run_owner_https_diagnostic import _lock_environment

    _lock_environment()
    import time

    from api.beta_owner_console import _local_console

    if not _local_console():
        raise _Refused
    from api.beta_live_record import LiveEvidenceBinding, live_record_view
    from api.beta_owner_custody import Custody
    from api.beta_owner_operator import open_owner_binding
    from src.interop.epic import beta_live_staging as files
    from src.interop.epic.beta_catalog import Catalog
    from src.interop.epic.beta_live_generations import LiveGenerations

    allowlist = files.LiveAllowlist({args.alias: args.patient_id})
    evidence = LiveEvidenceBinding(args.attempt_id, args.generation_id, args.manifest_sha256, args.bundle_sha256)
    store = open_owner_binding(args.binding_root, args.binding_sha256, enabled=True, policy_accepted=True)
    try:
        owner = store.load()
        if owner.credential is None or owner.revoked:
            raise _Refused
        with Custody(args.private_root) as custody:
            staging_root = args.private_root / "staging"
            generation_root = args.private_root / "generations"
            catalog_path = args.private_root / "catalog.sqlite"
            files.verify_private_path(args.private_root)
            for suffix in ("-journal", "-wal", "-shm"):
                if os.path.lexists(catalog_path.with_name(catalog_path.name + suffix)):
                    raise _Refused
            for folder in (staging_root, generation_root):
                if not folder.is_dir() or files._is_link(folder.lstat()):
                    raise _Refused
                files.verify_private_path(folder)
            custody.file(catalog_path, writable=False)
            files.verify_private_path(catalog_path)
            catalog = Catalog(catalog_path, time.time, initialize=False, readonly=True)
            try:
                staging = files.LiveStaging(catalog, staging_root, initialize=False)
                generations = LiveGenerations(
                    catalog, generation_root, allowlist, staging, real_promotion=True, initialize=False
                )
                view = live_record_view(
                    generations,
                    args.alias,
                    authorized=lambda: not store.load().revoked,
                    genuine_only=True,
                    approved=evidence,
                )
                custody.check()
            finally:
                catalog.close()
        return view
    finally:
        store.close()


def _write(args: argparse.Namespace, view: dict) -> str:
    import time

    from api.beta_publication import publication_document
    from src.interop.epic import beta_live_staging as files

    files.verify_private_path(args.out.parent)
    document = publication_document(view, exported_at=time.time())
    data = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload = data.encode("utf-8")
    with open(args.out, "xb") as stream:  # never overwrite
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    files.verify_private_path(args.out)
    return hashlib.sha256(payload).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--accept-owner-policy", action="store_true")
    for name in ("binding-root", "private-root", "out"):
        parser.add_argument("--" + name, type=Path)
    for name in (
        "binding-sha256",
        "alias",
        "patient-id",
        "attempt-id",
        "generation-id",
        "manifest-sha256",
        "bundle-sha256",
    ):
        parser.add_argument("--" + name)
    try:
        args = parser.parse_args(argv)
        if args.accept_owner_policy is not True:
            print("Owner Epic publication export is disabled. Separate approval is required.")
            return 2
        _validate(args)
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        view = _read_view(args)
        digest = _write(args, view)
        counts = view["provenance"].get("resource_counts") or {}
        print(
            f"Publication copy written ({sum(counts.values())} resources); SHA-256 {digest}. Review it before any publication."
        )
        return 0
    except KeyboardInterrupt:
        return 1
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    except Exception:
        print("Owner Epic publication export refused or failed. Review is required.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
