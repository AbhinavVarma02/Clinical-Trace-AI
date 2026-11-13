"""Owner-attended local console confirmation. No call at import, no unattended authorization.

Real use requires separate approval. The offline tests substitute this entire OS boundary; they do not
inspect an actual SID, console session or credential. Same-principal processes and OS administrators
remain trusted. This confirms owner intent, not TPM/manufacturer attestation or rollback resistance.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import time
from ctypes import wintypes
from typing import Callable

from api.beta_webauthn_store import current_principal


def _local_console() -> bool:
    if sys.platform != "win32" or not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    mode, session = wintypes.DWORD(), wintypes.DWORD()
    for stream in (sys.stdin, sys.stdout):
        if not kernel.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)):
            return False
    if (
        not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
        or session.value in (0, 0xFFFFFFFF)
        or session.value != kernel.WTSGetActiveConsoleSessionId()
        or user.GetSystemMetrics(0x1000)  # SM_REMOTESESSION
    ):
        return False
    processes = (wintypes.DWORD * 64)()
    count = kernel.GetConsoleProcessList(processes, 64)
    return 0 < count <= 64 and os.getpid() in processes[:count]


def confirm_owner_action(
    principal: str,
    fingerprint: str,
    *,
    enabled: bool = False,
    diagnostic: Callable[[str, int], None] | None = None,
) -> bool:
    """Default deny BEFORE OS identity access. Fingerprint is a nonsecret comparison label, never a grant."""
    if enabled is not True or not principal or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        return False
    outcome = "console_initial_refused"
    elapsed_ms = 0
    try:
        if not _local_console():
            return False
        outcome = "principal_initial_refused"
        if current_principal() != principal:
            return False
        outcome = "input_failed"
        started = time.monotonic()
        print("Clinical-Trace owner action at https://owner.clinical-trace.localhost:8443")
        print("Compare this ENTIRE fingerprint with the intended browser/configuration:", fingerprint)
        response = input("Type approve followed by a space and the entire fingerprint: ")
        if response != "approve " + fingerprint:
            outcome = "response_mismatch"
            if diagnostic is not None:
                elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            return False
        elapsed = time.monotonic() - started
        elapsed_ms = max(0, int(elapsed * 1000))
        outcome = "prompt_expired"
        if not elapsed < 60:
            return False
        outcome = "console_recheck_refused"
        if not _local_console():
            return False
        outcome = "principal_recheck_refused"
        if current_principal() != principal:
            return False
        outcome = "approved"
        return True
    except Exception:
        return False
    finally:
        if diagnostic is not None:
            try:
                diagnostic(outcome, elapsed_ms)
            except Exception:
                pass  # Observation cannot turn a refusal into authorization.
