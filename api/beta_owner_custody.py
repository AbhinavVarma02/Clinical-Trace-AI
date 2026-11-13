"""Windows/NTFS-only object custody for the offline owner store and TLS loader.

No fallback to pathname snapshots on unsupported systems. Ancestors are acquired top down, checked
through handles, and held without delete sharing. ACL checks additionally exclude untrusted metadata
mutation (sharing alone does not protect ACLs/reparse metadata). Owner/SYSTEM/admin remain trusted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from src.interop.epic import beta_live_staging as private


class CustodyError(Exception):
    """Content-free, fail-closed custody refusal."""


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as wt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr = ctypes.c_void_p

    class FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wt.DWORD),
            ("created", wt.FILETIME),
            ("accessed", wt.FILETIME),
            ("written", wt.FILETIME),
            ("volume", wt.DWORD),
            ("size_high", wt.DWORD),
            ("size_low", wt.DWORD),
            ("links", wt.DWORD),
            ("index_high", wt.DWORD),
            ("index_low", wt.DWORD),
        ]

    kernel.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ptr, wt.DWORD, wt.DWORD, wt.HANDLE]
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wt.HANDLE, ctypes.POINTER(FileInfo)]
    kernel.GetFileInformationByHandle.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    kernel.GetDriveTypeW.argtypes = [wt.LPCWSTR]
    kernel.GetDriveTypeW.restype = wt.UINT
    kernel.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD, ptr, ptr, ptr, wt.LPWSTR, wt.DWORD]
    kernel.GetVolumeInformationW.restype = wt.BOOL
    advapi.GetSecurityInfo.argtypes = [wt.HANDLE, ctypes.c_int, wt.DWORD, ptr, ptr, ptr, ptr, ptr]
    advapi.GetSecurityInfo.restype = wt.DWORD

    def _acl(handle: int, *, private_leaf: bool) -> None:
        owner, dacl, descriptor = ptr(), ptr(), ptr()
        if advapi.GetSecurityInfo(
            handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        ):
            raise CustodyError
        try:
            trusted = {private._user_sid(), "S-1-5-18", "S-1-5-32-544"}
            # Windows service identity that owns standard protected filesystem ancestors.
            installer = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
            if not private_leaf:
                trusted.add(installer)
            if private._sid_text(owner.value) not in trusted or not dacl.value:
                raise CustodyError
            allowed = trusted | {"S-1-3-0", "S-1-3-4"}
            size = private._AclSizeInformation()
            if not private._advapi32.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
                raise CustodyError
            for index in range(size.AceCount):
                ace = ptr()
                if not private._advapi32.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                    raise CustodyError
                kind, flags = (ctypes.c_ubyte.from_address(ace.value + offset).value for offset in (0, 1))
                if flags & 8 or kind == 1:  # inherit-only has no effect here; a deny only restricts
                    continue
                if kind != 0:
                    raise CustodyError
                mask = wt.DWORD.from_address(ace.value + 4).value
                sid = private._sid_text(ace.value + 8)
                # Public create-child rights on ancestors do not mutate an existing pinned component.
                # Metadata, ownership, ACL and delete-child rights do. Private leaves allow no outsiders.
                mutation = 0x10000000 | 0x40000000 | 0x000D0152
                if sid not in allowed and (private_leaf or mask & mutation):
                    raise CustodyError
        finally:
            private._kernel32.LocalFree(descriptor)


class Custody:
    def __init__(self, directory: Path, *, private_leaf: bool = True) -> None:
        # Ancestor-only mode holds mutation checks for child creation and cannot pin private files.
        self.directory = directory
        self._private_leaf = private_leaf
        self._pins: list[tuple[Path, int, tuple[int, int, int], bool, bool]] = []
        self._closed = False
        try:
            if sys.platform != "win32":
                raise CustodyError
            if (
                not directory.is_absolute()
                or len(directory.drive) != 2
                or directory.drive[1] != ":"
                or any(
                    part in ("..", ".") or part.endswith((" ", ".")) or ":" in part or os.path.isreserved(part)
                    for part in directory.parts[1:]
                )
            ):
                raise CustodyError
            drive = directory.anchor
            filesystem = ctypes.create_unicode_buffer(32)
            if (
                kernel.GetDriveTypeW(drive) != 3
                or not kernel.GetVolumeInformationW(drive, None, 0, None, None, None, filesystem, 32)
                or filesystem.value != "NTFS"
            ):
                raise CustodyError
            for component in (*reversed(directory.parents), directory):
                self._pin(component, directory=True, private_leaf=private_leaf and component == directory)
                if (component / ".git").exists():
                    raise CustodyError
        except Exception:
            self.close()
            raise CustodyError from None

    def _info(self, handle: int, *, directory: bool, private_leaf: bool) -> tuple[int, int, int]:
        info = FileInfo()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise CustodyError
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory or (not directory and info.links != 1):
            raise CustodyError
        _acl(handle, private_leaf=private_leaf)
        return info.volume, info.index_high, info.index_low

    def _pin(
        self, path: Path, *, directory: bool, private_leaf: bool, create: bool = False, writable: bool = False
    ) -> None:
        if self._closed:
            raise CustodyError
        # No DELETE sharing; certificate/key pins also deny data writers. Metadata rights are ACL-gated.
        handle = kernel.CreateFileW(
            str(path), 0x80000000, 3 if writable else 1, None, 1 if create else 3, 0x02200000, None
        )
        if handle == ctypes.c_void_p(-1).value:
            raise CustodyError
        try:
            identity = self._info(handle, directory=directory, private_leaf=private_leaf)
            self._pins.append((path, handle, identity, directory, private_leaf))
        except Exception:
            kernel.CloseHandle(handle)
            raise

    def file(self, path: Path, *, create: bool = False, writable: bool = False) -> None:
        # A file must be a direct child of the private pinned directory.
        if (
            not self._private_leaf
            or not self._pins
            or path.parent != self.directory
            or ":" in path.name
            or path.name.endswith((" ", "."))
            or os.path.isreserved(path.name)
        ):
            raise CustodyError
        self._pin(path, directory=False, private_leaf=True, create=create, writable=writable)

    def check(self) -> None:
        if self._closed:
            raise CustodyError
        try:
            for _, handle, identity, directory, private_leaf in self._pins:
                if self._info(handle, directory=directory, private_leaf=private_leaf) != identity:
                    raise CustodyError
        except Exception:
            raise CustodyError from None

    def close(self) -> None:
        for _, handle, _, _, _ in reversed(self._pins):
            kernel.CloseHandle(handle)
        self._pins.clear()
        self._closed = True

    def __enter__(self) -> Custody:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
