"""Explicitly provisioned, single-process owner credential store. No automatic first-run state.

Only the public credential, counter and permanent revocation tombstone are durable. No secrets, grants,
challenges, cookies or elevations are stored. Missing/damaged state is never initialized by opening it.
Filesystem custody assumes a trusted owner/OS administrator; it is not protection against their backups
or rollback. Genuine-purpose provisioning is separately default-denied. Actual recovery/enrollment/LIVE
activation require independent review and owner approval.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from api.beta_owner_custody import Custody
from src.interop.epic import beta_live_staging as private

if TYPE_CHECKING:
    from api.beta_webauthn import LocalOwnerPolicy

SCHEMA = """CREATE TABLE owner_state (
id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
origin TEXT NOT NULL, handle BLOB NOT NULL, principal TEXT NOT NULL, purpose TEXT NOT NULL,
revision INTEGER NOT NULL, credential BLOB, counter INTEGER NOT NULL, revoked INTEGER NOT NULL)"""
PURPOSE = "offline-synthetic-v1"
# A distinct binding, never a migration or relabeling of an offline credential database.
GENUINE_PURPOSE = "owner-attended-platform-uv-read-v1"


class StoreUnavailable(Exception):
    """Content-free refusal. Never include a path, public credential or SQL exception in a response/log."""


@dataclass(frozen=True, repr=False)
class OwnerState:
    revision: int = 0
    credential: bytes | None = None
    counter: int = 0
    revoked: bool = False


def current_principal() -> str:
    # OS process identity, not a caller-supplied HTTP header or environment username.
    if sys.platform == "win32":
        return private._user_sid()
    return f"uid:{os.getuid()}"


def private_path(path: Path, *, directory: bool = False) -> tuple[int, int]:
    """Refuse reparse points in every component, hard links, Git locations and unverifiable ACLs."""
    if not path.is_absolute():
        raise StoreUnavailable
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise StoreUnavailable
        if (component / ".git").exists():
            raise StoreUnavailable
    info = path.lstat()
    valid_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not valid_kind:
        raise StoreUnavailable
    private.verify_private_path(path)
    return info.st_dev, info.st_ino


class OwnerCredentialStore:
    """Exclusive SQLite custody with FULL durability, strict binding, CAS transitions and failure latching.

    Provisioning is an explicit OS-owner operation, default denied, into a NEW private directory. This
    store has separate synthetic and genuine-purpose default-denied provisioners. There is no
    recovery/reset/reenroll API or synthetic-to-genuine conversion.
    """

    @classmethod
    def provision(
        cls,
        root: Path,
        policy: LocalOwnerPolicy,
        principal: str,
        *,
        authorize: Callable[[], bool] | None = None,
        synthetic_only: bool = False,
    ) -> OwnerCredentialStore:
        try:
            if policy.purpose != PURPOSE or synthetic_only is not True or authorize is None or authorize() is not True:
                raise StoreUnavailable
            if not principal or current_principal() != principal:
                raise StoreUnavailable
            with Custody(root.parent):
                root.mkdir(mode=0o700)  # existing/partial state is never reused
                return cls(root, policy, principal, _initialize=True)
        except Exception:
            # A partial initialization is intentionally left unusable; never erase/reset it automatically.
            raise StoreUnavailable from None

    @classmethod
    def provision_genuine(
        cls,
        root: Path,
        policy: LocalOwnerPolicy,
        principal: str,
        *,
        authorize: Callable[[], bool] | None = None,
    ) -> OwnerCredentialStore:
        """Trusted local provisioner only; no HTTP/CLI/env switch or implicit first-run initialization.

        The genuine integration supplies an attended-console confirmation after separate owner approval.
        A denied/default call performs no identity, custody or filesystem operation.
        """
        try:
            if policy.purpose != GENUINE_PURPOSE or authorize is None or authorize() is not True:
                raise StoreUnavailable
            if not principal or current_principal() != principal:
                raise StoreUnavailable
            with Custody(root.parent):
                root.mkdir(mode=0o700)
                return cls(root, policy, principal, _initialize=True)
        except Exception:
            raise StoreUnavailable from None

    def __init__(self, root: Path, policy: LocalOwnerPolicy, principal: str, *, _initialize: bool = False) -> None:
        self.root, self.path = root, root / "owner.sqlite3"
        self.policy, self.principal = policy, principal
        self._failed = False
        self._connection: sqlite3.Connection | None = None
        self._custody: Custody | None = None
        try:
            if not principal or current_principal() != principal:
                raise StoreUnavailable
            self._custody = Custody(root)
            self._custody.file(self.path, create=_initialize, writable=True)
            self._root_identity = private_path(root, directory=True)
            self._file_identity = private_path(self.path)
            # SQLite can recover at connect time. Only its empty, inert DELETE-journal is admissible;
            # nonempty journals/WAL/SHM need reviewed recovery. Hold its custody while checking size.
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(str(self.path) + suffix)
                if os.path.lexists(sidecar):
                    with Custody(root) as sidecar_custody:
                        sidecar_custody.file(sidecar)
                        if suffix != "-journal" or sidecar.stat().st_size != 0:
                            raise StoreUnavailable
            self._guard()
            self._connection = sqlite3.connect(
                self.path.as_uri() + "?mode=rw", uri=True, timeout=0, isolation_level=None
            )
            self._connection.execute("PRAGMA trusted_schema=OFF")
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA journal_size_limit=0")
            self._connection.execute("PRAGMA synchronous=FULL")
            if _initialize:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(SCHEMA)
                self._connection.execute(
                    "INSERT INTO owner_state VALUES (1,1,?,?,?,?,0,NULL,0,0)",
                    (policy.origin, policy.owner_handle, principal, policy.purpose),
                )
                self._connection.execute("COMMIT")
            self._connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            self._connection.execute("BEGIN EXCLUSIVE")
            self._connection.execute("COMMIT")  # EXCLUSIVE mode retains the OS lock through close/process exit
            self.load()
        except Exception:
            self.close()
            raise StoreUnavailable from None

    def _guard(self) -> None:
        if self._custody is None:
            raise StoreUnavailable
        self._custody.check()
        if (
            self._failed
            or current_principal() != self.principal
            or private_path(self.root, directory=True) != self._root_identity
            or private_path(self.path) != self._file_identity
            or os.path.lexists(self.root / "mutation.pending")
        ):
            raise StoreUnavailable
        for suffix in ("-journal", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if os.path.lexists(path):
                private_path(path)

    def load(self) -> OwnerState:
        try:
            self._guard()
            if self._connection is None:
                raise StoreUnavailable
            if self._connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise StoreUnavailable
            if self._connection.execute("SELECT type,name,sql FROM sqlite_master").fetchall() != [
                ("table", "owner_state", SCHEMA)
            ]:
                raise StoreUnavailable
            rows = self._connection.execute("SELECT * FROM owner_state").fetchall()
            if len(rows) != 1:
                raise StoreUnavailable
            ident, version, origin, handle, principal, purpose, revision, credential, counter, revoked = rows[0]
            if (
                (ident, version, origin, handle, principal, purpose)
                != (1, 1, self.policy.origin, self.policy.owner_handle, self.principal, self.policy.purpose)
                or type(revision) is not int
                or revision < 0
                or type(counter) is not int
                or not 0 <= counter <= 0xFFFFFFFF
                or type(revoked) is not int
                or revoked not in (0, 1)
                or (credential is not None and (type(credential) is not bytes or not 1 <= len(credential) <= 4096))
                or (credential is None and counter != 0)
                or (revision > 0 and credential is None and revoked == 0)
                or (revision == 0 and (credential is not None or revoked != 0))
            ):
                raise StoreUnavailable
            return OwnerState(revision, credential, counter, bool(revoked))
        except Exception:
            self.close()  # SQLite closes before directory/file custody is released on refusal
            raise StoreUnavailable from None

    def _save(
        self, expected: OwnerState, next_state: OwnerState, *, valid: Callable[[], bool] | None = None
    ) -> OwnerState:
        try:
            if self.load() != expected or expected.revoked or self._connection is None:
                raise StoreUnavailable
            marker = self.root / "mutation.pending"
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"review-required-if-present\n")
                stream.flush()
                os.fsync(stream.fileno())
            private_path(marker)
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "UPDATE owner_state SET revision=?,credential=?,counter=?,revoked=? WHERE id=1 AND revision=?",
                (
                    next_state.revision,
                    next_state.credential,
                    next_state.counter,
                    int(next_state.revoked),
                    expected.revision,
                ),
            )
            self._connection.execute("COMMIT")  # commit MUST precede granting a session/elevation or revocation success
            # Check genuine enrollment authority AFTER COMMIT, before removing the existing refusal marker.
            # A late/failed check leaves that marker intact across restart; no second write is needed.
            if valid is not None and valid() is not True:
                raise StoreUnavailable
            marker.unlink()
            private._sync_directory(self.root)
            if self.load() != next_state:
                raise StoreUnavailable
            return next_state
        except Exception:
            # An ambiguous/crashed write leaves mutation.pending. Restart refuses until reviewed recovery.
            # No recovery routine exists in this increment. Do not log the underlying exception.
            self.close()
            raise StoreUnavailable from None

    def enroll(
        self, expected: OwnerState, credential: bytes, counter: int, *, valid: Callable[[], bool] | None = None
    ) -> OwnerState:
        if expected.credential is not None or expected.revoked:
            raise StoreUnavailable
        return self._save(expected, OwnerState(expected.revision + 1, credential, counter), valid=valid)

    def advance(self, expected: OwnerState, counter: int) -> OwnerState:
        if expected.credential is None or ((expected.counter or counter) and counter <= expected.counter):
            raise StoreUnavailable
        return self._save(expected, replace(expected, revision=expected.revision + 1, counter=counter))

    def revoke(self, expected: OwnerState) -> OwnerState:
        return self._save(expected, replace(expected, revision=expected.revision + 1, revoked=True))

    def close(self) -> None:
        self._failed = True
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                return  # retain pins if SQLite closure is uncertain; only retry close/process exit can release
            self._connection = None
        if self._custody is not None:
            self._custody.close()
