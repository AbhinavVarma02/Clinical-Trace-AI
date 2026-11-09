"""Immutable LIVE patient allowlist and private, short-lived staging of validated LIVE candidates.

A staged candidate is the normalized bundle and manifest of one validated LIVE acquisition. It exists only so
that an explicit, hash-bound owner approval can promote it (see ``beta_live_generations``). Nothing here
serves or indexes a candidate, and nothing here is reachable from a browser route. Retention is at most
``STAGING_RETENTION_S``; erasure happens earlier on revocation, supersession or owner request.

Persisting any candidate requires ``verify_private_path`` to pass first, whatever its label: on Windows that is a
real ACL check (only the current user, SYSTEM and Administrators may be granted access); on POSIX an owner and
mode check.

Erasure means the files and directory are removed and the catalog row is blanked. It is not a forensic wipe
(no overwrite): on journaling or flash storage deleted bytes can linger below the file system, and the
process may still hold the candidate in memory until it is released. Catalog rows are content-free.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from src.interop.epic.beta_acquisition import AcquisitionError
from src.interop.epic.beta_catalog import MOCK_PATIENTS, Catalog
from src.interop.epic.client import valid_fhir_id
from src.interop.epic.mapping import content_sha256

STAGING_RETENTION_S = 24 * 3600
MAX_CANDIDATE_BYTES = 20 * 1024 * 1024
MAX_LIVE_PATIENTS = 1  # owner decision: exactly one approved synthetic patient
ALIAS = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
ATTEMPT = re.compile(r"[A-Za-z0-9_-]{16,64}")
_REPARSE = 0x400


@dataclass(frozen=True)
class LiveAllowlist:
    """Explicit, validated, immutable approval of LIVE patients. The default is empty (nothing approved)."""

    patients: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.patients, Mapping) or len(self.patients) > MAX_LIVE_PATIENTS:
            raise ValueError("at most one approved LIVE patient is permitted")
        checked: dict[str, str] = {}
        for alias, patient in self.patients.items():
            if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or alias in MOCK_PATIENTS:
                raise ValueError("invalid LIVE alias")
            try:
                valid_fhir_id(patient)
            except (ValueError, TypeError):
                raise ValueError("invalid LIVE patient") from None
            if patient in {".", ".."} or patient in MOCK_PATIENTS.values() or patient in checked.values():
                raise ValueError("invalid LIVE patient")
            checked[alias] = patient
        object.__setattr__(self, "patients", MappingProxyType(checked))

    @classmethod
    def empty(cls) -> LiveAllowlist:
        return cls({})

    def get(self, alias: str) -> str | None:
        return self.patients.get(alias)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base.resolve())
    except ValueError:
        return False
    return True


def private_root(root: Path) -> Path:
    """Create and verify a private staging directory that is outside any Git work tree."""
    path = Path(root)
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise ValueError("LIVE staging must be outside Git work trees")
    if os.path.lexists(path) and _is_link(path.lstat()):
        raise ValueError("LIVE staging root must not be a link")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("LIVE staging root must be a plain directory")
    if sys.platform == "win32":
        # Windows ignores POSIX modes. Rely on the per-user ACL inherited from the profile or temp
        # directory, and refuse locations outside them rather than claim an ACL this code does not set.
        if not (_inside(resolved, Path.home()) or _inside(resolved, Path(tempfile.gettempdir()))):
            raise ValueError("LIVE staging must be inside the current user's profile or temp directory")
    else:
        os.chmod(path, 0o700)
        info = path.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("LIVE staging root must be private to the current user")
    return resolved


def _sync_directory(path: Path) -> None:
    if os.name != "nt":  # Windows has no portable directory fsync; same boundary as generations.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _remove(path: Path) -> bool:
    """Remove a file or directory tree without following links. True when nothing remains."""
    if not os.path.lexists(path):
        return True
    try:
        info = path.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            _unlink(path)
        else:
            with os.scandir(path) as entries:
                children = [Path(entry.path) for entry in entries]
            for child in children:
                _remove(child)
            try:
                path.rmdir()
            except PermissionError:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                path.rmdir()
    except OSError:
        pass
    return not os.path.lexists(path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)  # read-only attribute on Windows
        path.unlink()


# -- private-storage verification -----------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _pvoid = ctypes.c_void_p

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", _pvoid), ("Attributes", wintypes.DWORD)]

    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(_pvoid),
        ctypes.POINTER(_pvoid),
        ctypes.POINTER(_pvoid),
        ctypes.POINTER(_pvoid),
        ctypes.POINTER(_pvoid),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetAclInformation.argtypes = [_pvoid, _pvoid, wintypes.DWORD, ctypes.c_int]
    _advapi32.GetAclInformation.restype = wintypes.BOOL
    _advapi32.GetAce.argtypes = [_pvoid, wintypes.DWORD, ctypes.POINTER(_pvoid)]
    _advapi32.GetAce.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [_pvoid, ctypes.POINTER(ctypes.c_wchar_p)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        _pvoid,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.argtypes = [_pvoid]
    _kernel32.LocalFree.restype = _pvoid

    def _sid_text(sid: int | None) -> str:
        text = ctypes.c_wchar_p()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), "SID conversion failed")
        try:
            return text.value or ""
        finally:
            _kernel32.LocalFree(ctypes.cast(text, _pvoid))

    def _user_sid() -> str:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), "process token unavailable")
        try:
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # sizing call fails by design
            buffer = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise OSError(ctypes.get_last_error(), "token user unavailable")
            return _sid_text(_SidAndAttributes.from_buffer(buffer).Sid)
        finally:
            _kernel32.CloseHandle(token)

    def _windows_acl_problem(path: Path) -> str | None:
        """None when the DACL grants access only to the current user, SYSTEM and Administrators."""
        owner, dacl, descriptor = _pvoid(), _pvoid(), _pvoid()
        code = _advapi32.GetNamedSecurityInfoW(
            str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        )
        if code != 0:
            return "security descriptor unreadable"
        try:
            me = _user_sid()
            # The current user, SYSTEM, Administrators, and the CREATOR OWNER / OWNER RIGHTS placeholders, which
            # grant nothing beyond the (separately checked) owner. Python 3.13's mkdir(mode=0o700) produces
            # exactly such a restricted ACL on Windows; an older interpreter fails here unless the ACL is hardened.
            allowed = {me, "S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4"}
            if _sid_text(owner.value) not in {me, "S-1-5-32-544"}:
                return "unexpected owner"
            if not dacl.value:
                return "no DACL: everyone has access"
            info = _AclSizeInformation()
            if not _advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
                return "ACL unreadable"
            for index in range(info.AceCount):
                ace = _pvoid()
                if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)) or ace.value is None:
                    return "ACE unreadable"
                kind = ctypes.c_ubyte.from_address(ace.value).value
                if kind == 1:  # an explicit deny only restricts access
                    continue
                if kind != 0:
                    return "unsupported ACE type"
                sid = _sid_text(ace.value + 8)  # ACCESS_ALLOWED_ACE: header (4) + mask (4), then the SID
                if sid not in allowed:
                    return f"access granted to {sid}"
            return None
        finally:
            _kernel32.LocalFree(descriptor)


def acl_problem(path: Path) -> str | None:
    """Why ``path`` is not private to the current user, or None. Windows checks the real DACL; POSIX checks mode."""
    path = Path(path)
    if not os.path.lexists(path) or _is_link(path.lstat()):
        return "not a plain existing path"
    if sys.platform == "win32":
        return _windows_acl_problem(path)
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        return "accessible to other users"
    return None


def verify_private_path(path: Path) -> None:
    """Mandatory gate before LIVE-labelled data is persisted. Fails closed with a generic code."""
    if acl_problem(path) is not None:
        raise AcquisitionError("acl_unverified")


def make_private_directory(path: Path) -> None:
    """Create a new directory and verify it before the caller writes anything into it (S3).

    The caller must treat a failure as "directory exists, unverified" and remove it; nothing has been written yet.
    """
    path.mkdir(mode=0o700)
    verify_private_path(path)


def review_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Content-free description of a candidate or generation: provenance, counts, coverage, warnings."""
    raw_coverage = manifest.get("coverage")
    coverage: dict[str, Any] = raw_coverage if isinstance(raw_coverage, dict) else {}
    raw_operations = coverage.get("operations")
    operations: dict[str, Any] = raw_operations if isinstance(raw_operations, dict) else {}
    keep = ("execution", "import", "matches", "pages", "imported", "excluded", "rejected", "duplicates", "reason")
    return {
        "mode": manifest.get("mode"),
        "source": manifest.get("source"),
        "policy": manifest.get("policy"),
        "completeness": manifest.get("completeness"),
        "retrieved_at": manifest.get("retrieved_at"),
        "resource_counts": manifest.get("resource_counts"),
        "unresolved_references": coverage.get("unresolved_references"),
        "note_bodies": coverage.get("note_bodies"),
        "operations": {
            name: {
                **{k: counts[k] for k in keep if k in counts},
                **({"informational_codes": counts["informational_codes"]} if "informational_codes" in counts else {}),
            }
            for name, counts in operations.items()
            if isinstance(counts, dict)
        },
    }


class LiveStaging:
    """Candidate lifecycle: stage, expire, supersede, erase. All methods are synchronous (no awaits), so a
    revocation handler on the same loop can never interleave with a write."""

    def __init__(self, catalog: Catalog, root: Path) -> None:
        self.catalog = catalog
        self.root = private_root(root)
        with self.catalog.db:
            self.catalog.db.execute(
                """
                CREATE TABLE IF NOT EXISTS live_candidate (
                    attempt_id TEXT PRIMARY KEY, alias TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('writing','staged','erase_pending','erased')),
                    manifest_sha256 TEXT, bytes INTEGER, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    erased_at REAL, erase_reason TEXT
                )
                """
            )
        self.sweep()

    # -- queries ---------------------------------------------------------------------------------------------

    def view(self, alias: str) -> dict[str, Any] | None:
        """Content-free summary for the owner status route, including what the owner would be approving."""
        row = self.catalog.db.execute(
            "SELECT attempt_id, state, bytes, created_at, expires_at, manifest_sha256 FROM live_candidate "
            "WHERE alias = ? AND state IN ('staged','erase_pending') ORDER BY created_at DESC LIMIT 1",
            (alias,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        review = None
        if row["state"] == "staged":
            try:
                _, manifest, _ = self.read_candidate(row["attempt_id"], alias)
                review = review_summary(json.loads(manifest))
            except (AcquisitionError, ValueError):
                review = None
        result["review"] = review
        return result

    def read_candidate(self, attempt: str, alias: str) -> tuple[dict[str, Any], bytes, bytes]:
        """Hash-verified bytes of a staged, unexpired candidate, for an explicit promotion. Never served."""
        if not ATTEMPT.fullmatch(attempt) or not ALIAS.fullmatch(alias):
            raise AcquisitionError("staging_scope")
        row = self.catalog.db.execute(
            "SELECT * FROM live_candidate WHERE attempt_id = ? AND alias = ?", (attempt, alias)
        ).fetchone()
        if row is None or row["state"] != "staged":
            raise AcquisitionError("candidate_unavailable")
        if row["expires_at"] <= self.catalog.clock():
            raise AcquisitionError("candidate_expired")
        directory = self.root / attempt
        try:
            for path in (self.root, directory, directory / "manifest.json", directory / "normalized_bundle.json"):
                if _is_link(path.lstat()):
                    raise ValueError
            manifest = (directory / "manifest.json").read_bytes()
            bundle = (directory / "normalized_bundle.json").read_bytes()
        except (OSError, ValueError):
            raise AcquisitionError("candidate_integrity") from None
        if content_sha256(manifest) != row["manifest_sha256"] or len(manifest) + len(bundle) != row["bytes"]:
            raise AcquisitionError("candidate_integrity")
        return dict(row), manifest, bundle

    # -- writes ----------------------------------------------------------------------------------------------

    def _admit(self, alias: str, authorized: Callable[[], bool]) -> None:
        if self.catalog.revoked("owner") or self.catalog.revoked(alias) or not authorized():
            raise AcquisitionError("authorization_revoked")

    def stage(self, attempt: str, alias: str, manifest: bytes, bundle: bytes, authorized: Callable[[], bool]) -> None:
        if not ATTEMPT.fullmatch(attempt) or not ALIAS.fullmatch(alias):
            raise AcquisitionError("staging_scope")
        if len(manifest) + len(bundle) > MAX_CANDIDATE_BYTES:
            raise AcquisitionError("byte_limit")
        try:
            if not isinstance(json.loads(manifest), dict):
                raise ValueError
        except ValueError:
            raise AcquisitionError("staging_scope") from None
        # Unconditional. This store exists only for LIVE-policy attempts, and no label the caller controls (the mode
        # string, its case, duplicate JSON keys, a mock transport that proxies real traffic) may decide whether
        # storage is checked before bytes are written.
        verify_private_path(self.root)
        self._admit(alias, authorized)
        self.erase_alias(alias, "superseded")  # one candidate per alias; the newer replaces the older
        now = self.catalog.clock()
        with self.catalog.db:
            self.catalog.db.execute(
                "INSERT INTO live_candidate (attempt_id, alias, state, created_at, expires_at) "
                "VALUES (?, ?, 'writing', ?, ?)",
                (attempt, alias, now, now + STAGING_RETENTION_S),
            )
        temporary, final = self.root / f"{attempt}.tmp", self.root / attempt
        try:
            make_private_directory(temporary)  # verified before the first byte is written
            _write(temporary / "manifest.json", manifest)
            _write(temporary / "normalized_bundle.json", bundle)
            _sync_directory(temporary)
            verify_private_path(temporary)
            self._admit(alias, authorized)  # last boundary, still without an await
            os.replace(temporary, final)
            _sync_directory(self.root)
            with self.catalog.db:
                self.catalog.db.execute(
                    "UPDATE live_candidate SET state = 'staged', manifest_sha256 = ?, bytes = ? WHERE attempt_id = ?",
                    (content_sha256(manifest), len(manifest) + len(bundle), attempt),
                )
                self.catalog._audit("live_candidate_staged", attempt)
        except BaseException as exc:
            self._erase(attempt, getattr(exc, "code", "write_failed"))
            if isinstance(exc, AcquisitionError) or not isinstance(exc, Exception):
                raise
            raise AcquisitionError("staging_failed") from None

    # -- erasure ---------------------------------------------------------------------------------------------

    def _erase(self, attempt: str, reason: str) -> bool:
        row = self.catalog.db.execute("SELECT state FROM live_candidate WHERE attempt_id = ?", (attempt,)).fetchone()
        if row is None or row["state"] == "erased":
            return True
        gone = ATTEMPT.fullmatch(attempt) is not None and all(
            _remove(self.root / name) for name in (attempt, f"{attempt}.tmp")
        )
        now = self.catalog.clock()
        with self.catalog.db:
            if gone:
                self.catalog.db.execute(
                    "UPDATE live_candidate SET state = 'erased', manifest_sha256 = NULL, bytes = NULL, "
                    "erased_at = ?, erase_reason = ? WHERE attempt_id = ?",
                    (now, reason[:48], attempt),
                )
                self.catalog._audit("live_candidate_erased", attempt)
            else:
                self.catalog.db.execute(
                    "UPDATE live_candidate SET state = 'erase_pending' WHERE attempt_id = ?", (attempt,)
                )
                self.catalog._audit("live_erase_failed", attempt)
        return gone

    def _erase_where(self, clause: str, params: tuple[Any, ...], reason: str) -> int:
        rows = self.catalog.db.execute(
            f"SELECT attempt_id FROM live_candidate WHERE state != 'erased' AND {clause}", params
        ).fetchall()
        return sum(1 for row in rows if self._erase(row["attempt_id"], reason))

    def erase_attempt(self, attempt: str, reason: str) -> bool:
        return self._erase(attempt, reason)

    def erase_alias(self, alias: str, reason: str) -> int:
        return self._erase_where("alias = ?", (alias,), reason)

    def erase_all(self, reason: str) -> int:
        return self._erase_where("1 = 1", (), reason)

    def purge(self) -> int:
        """Erase expired, revoked, interrupted and previously failed-to-erase candidates."""
        now, count = self.catalog.clock(), 0
        owner_revoked = self.catalog.revoked("owner")
        for row in self.catalog.db.execute(
            "SELECT attempt_id, alias, state, expires_at FROM live_candidate WHERE state != 'erased'"
        ).fetchall():
            if owner_revoked:
                reason = "owner_revoked"
            elif self.catalog.revoked(row["alias"]):
                reason = "revoked"
            elif row["state"] == "writing":
                reason = "interrupted"
            elif row["state"] == "erase_pending":
                reason = "retry"
            elif row["expires_at"] <= now:
                reason = "expired"
            else:
                continue
            count += self._erase(row["attempt_id"], reason)
        return count

    def sweep(self) -> None:
        """Startup recovery: apply retention and revocation, then remove anything the catalog does not own."""
        self.purge()
        staged = {
            row["attempt_id"]
            for row in self.catalog.db.execute("SELECT attempt_id FROM live_candidate WHERE state = 'staged'")
        }
        with os.scandir(self.root) as entries:
            found = [(entry.name, Path(entry.path)) for entry in entries]
        for name, path in found:
            if name not in staged:
                _remove(path)
                self.catalog.audit("live_orphan_erased")
