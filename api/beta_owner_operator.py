"""Default-denied owner binding and same-event-loop auth-only operator. No CLI/env identity discovery.

All real calls need separate owner approval. No Epic credentials, acquisition, promotion or LIVE activation
is provided. The caller supplies the accepted binding digest and exact trusted HTTPS configuration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from api.beta_owner_console import _local_console
from api.beta_owner_custody import Custody
from api.beta_owner_genuine import GenuineOwnerApp
from api.beta_webauthn import ENROLLMENT_S, OWNER_ORIGIN, LocalOwnerPolicy
from api.beta_webauthn_store import GENUINE_PURPOSE, OwnerCredentialStore, private_path
from src.interop.epic.beta_acquisition import object_json
from src.interop.epic.beta_catalog import RefreshRejected

BINDING = "owner-binding.json"


def save_owner_binding(store: OwnerCredentialStore, *, enabled: bool = False) -> str:
    """Write once, after attended provisioning, in the same private directory; return only a public digest.

    The separately accepted digest is the reopen input. Never discover another binding or overwrite/reset.
    Partial writes remain unusable and require review. The binding contains no private authenticator key.
    """
    if enabled is not True or type(store) is not OwnerCredentialStore or store.policy.purpose != GENUINE_PURPOSE:
        raise ValueError("owner binding write is disabled")
    store.load()
    payload = json.dumps(
        {
            "version": 1,
            "origin": OWNER_ORIGIN,
            "purpose": GENUINE_PURPOSE,
            "principal": store.principal,
            "handle": store.policy.owner_handle.hex(),
        },
        sort_keys=True,
    ).encode()
    with Custody(store.root) as custody:
        path = store.root / BINDING
        custody.file(path, create=True, writable=True)
        with path.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        private_path(path)
        custody.check()
    return hashlib.sha256(payload).hexdigest()


def open_owner_binding(
    root: Path, expected_sha256: str, *, enabled: bool = False, policy_accepted: bool = False
) -> OwnerCredentialStore:
    if (
        enabled is not True
        or policy_accepted is not True
        or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
        or not root.is_absolute()
    ):
        raise ValueError("approved owner binding is required")
    with Custody(root) as custody:
        path = root / BINDING
        custody.file(path)
        if path.stat().st_size > 4096:
            raise ValueError("owner binding refused")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("owner binding refused")
        binding = object_json(raw)
        if (
            set(binding) != {"version", "origin", "purpose", "principal", "handle"}
            or type(binding["version"]) is not int
            or binding["version"] != 1
            or binding["origin"] != OWNER_ORIGIN
            or binding["purpose"] != GENUINE_PURPOSE
            or not isinstance(binding["principal"], str)
            or not binding["principal"]
            or not isinstance(binding["handle"], str)
            or not re.fullmatch(r"(?:[a-f0-9]{2}){16,64}", binding["handle"])
        ):
            raise ValueError("owner binding refused")
        policy = LocalOwnerPolicy(OWNER_ORIGIN, bytes.fromhex(binding["handle"]), GENUINE_PURPOSE)
        custody.check()
        return OwnerCredentialStore(root, policy, binding["principal"])


COMMAND_PROMPT = "Owner command (enroll, revoke or stop), then Enter: "
# Fixed, non-secret refusal guidance keyed by the existing RefreshRejected codes. Display only.
ENROLLMENT_REFUSALS = {
    "enrollment_rejected": (
        f"Enrollment refused: the browser request expired ({ENROLLMENT_S}-second limit) or was already used. "
        "Type enroll first, then click Request owner enrollment once."
    ),
    "enrollment_not_authorized": (
        f"Enrollment refused: the console confirmation was not typed exactly within {ENROLLMENT_S} seconds."
    ),
}
REVOCATION_REFUSALS = {
    "revocation_not_authorized": (
        f"Revocation refused: the console confirmation was not typed exactly within {ENROLLMENT_S} seconds. "
        "Nothing was revoked. Type revoke to try again."
    ),
}
# Arming only waits for the owner's own next browser request; it approves nothing. The unchanged
# approve_enrollment/confirm_owner_action path (60-second request window, exact fingerprint) still decides.
ARM_S = 120
ARM_RESPONSE_S = 1.0  # let the browser receive its enroll/pending response before the console prompt blocks
RUN_WARNING_S = 60  # one notice before the bounded listener stops itself
# A console confirmation blocks this single event loop in input(); if the owner never answers, the loop
# cannot observe the bound. This independent timer then ends the process. Nothing is approved or written
# while a confirmation is waiting. Grace exceeds the 60 s confirmation window plus margin: a prompt can only open
# before the bound and must be answered within 60 s, so any confirmation-driven store write completes well before
# the timer, and the existing 5 s graceful shutdown ends HTTP-driven work first; a kill can then only land while
# the loop is blocked waiting for console input (never mid-write).
HARD_STOP_GRACE_S = 90
HARD_STOP_EXIT = 3
_monotonic = time.monotonic  # wall-clock-independent run bound; patchable in tests only
_hard_exit = os._exit  # patchable in tests only


def _hard_stop() -> None:
    print("\nRun limit passed while waiting for console input; the listener process is ending now.", flush=True)
    _hard_exit(HARD_STOP_EXIT)


def _poll_command(buffer: list[str]) -> str | None:
    """Read only from the verified local console, without a background input thread or SQLite dispatch.

    Accepted command letters are echoed to the same console (commands are not secrets); acceptance rules
    are unchanged. A cleared entry starts a fresh visible prompt so swallowed or invalid keys are evident.
    """
    if not _local_console():
        raise ValueError("attended local console required")
    import msvcrt

    def echo(text: str) -> None:
        for char in text:
            msvcrt.putwch(char)

    while msvcrt.kbhit():
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            msvcrt.getwch()
            buffer.clear()
            echo(" [cleared]\r\n" + COMMAND_PROMPT)
        elif char in ("\r", "\n"):
            command = "".join(buffer)
            buffer.clear()
            echo("\r\n")
            return command
        elif char == "\b":
            if buffer:
                buffer.pop()
                echo("\b \b")
        elif char in "abcdefghijklmnopqrstuvwxyz" and len(buffer) < 8:
            buffer.append(char)
            echo(char)
        else:
            buffer.clear()
            echo(" [cleared]\r\n" + COMMAND_PROMPT)
    return None


async def serve_owner_auth(
    app: GenuineOwnerApp,
    config: Any,
    *,
    enabled: bool = False,
    enrollment_approved: bool = False,
    revocation_approved: bool = False,
    stop_after_s: float | None = None,
) -> bool:
    """Auth-only execution entry; fixed bootstrap config, single event loop, separately approved actions.

    Browser creation, provisioning and LIVE reads are absent. The owner explicitly types enroll/revoke,
    then completes the existing full fingerprint confirmation. Startup never grants enrollment.
    stop_after_s bounds the attended run; reaching it only stops the listener (never approves anything).
    Returns True only when the run limit (not the owner) ended the listener.
    """
    if enabled is not True or type(app) is not GenuineOwnerApp:
        raise ValueError("owner auth execution is disabled")
    if stop_after_s is not None and not (type(stop_after_s) in (int, float) and 0 < stop_after_s <= 600):
        raise ValueError("bounded attended run required")
    if (
        app._live_reads_approved
        or app._reads is not None
        or config.app is not app
        or config.loaded_app is not app
        or config.host != "127.0.0.1"
        or config.port != 8443
        or config.workers != 1
        or config.reload
        or config.proxy_headers
        or config.access_log
        or config.ssl is None
        or config.log_level != "warning"
        or not _local_console()
    ):
        raise ValueError("auth-only trusted local configuration required")
    app._auth._owner_available()
    import uvicorn

    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    buffer: list[str] = []
    armed_until = 0.0

    def approve_current() -> None:
        identifier, context = next(iter(app._enrollments.items()))
        left = max(0, int(context.expires - app._auth.clock()))
        print(f"Browser enrollment request: {left} of {ENROLLMENT_S} seconds left, including Windows Hello.")
        try:
            app.approve_enrollment(identifier, context.fingerprint, enabled=True)
            print("Internal enrollment approved for that browser request only.")
            print("Next: click Complete approved enrollment and finish Windows Hello now.")
        except RefreshRejected as exc:
            reason = ENROLLMENT_REFUSALS.get(exc.code, "Enrollment refused; review is required.")
            print(f"{reason} [{exc.code}]")
        except Exception:
            print("Enrollment refused; review is required.")

    print("Owner auth only. Console commands: enroll, revoke, stop. LIVE reads remain disabled.")
    started = _monotonic()
    warned = limit_reached = False
    watchdog: threading.Timer | None = None
    if stop_after_s is not None:
        print(f"This listener stops by itself after {int(stop_after_s // 60)} minutes.")
        watchdog = threading.Timer(stop_after_s + HARD_STOP_GRACE_S, _hard_stop)
        watchdog.daemon = True
        watchdog.start()
    print(COMMAND_PROMPT, end="", flush=True)
    try:
        while not task.done() and not server.should_exit:
            app._store.load()  # approved OS principal/custody/state must remain valid for operator dispatch
            if stop_after_s is not None:
                elapsed = _monotonic() - started
                if elapsed >= stop_after_s:
                    print("\nRun limit reached; the listener is stopping.")
                    limit_reached = server.should_exit = True
                    break
                if not warned and elapsed >= stop_after_s - RUN_WARNING_S:
                    warned = True
                    print("\nOne minute left before this listener stops.")
                    print(COMMAND_PROMPT, end="", flush=True)
            command = _poll_command(buffer)
            if command == "stop":
                server.should_exit = True
            elif command == "enroll":
                armed_until = 0.0
                if enrollment_approved is not True:
                    print("Enrollment is not approved.")
                elif len(app._enrollments) == 1:
                    approve_current()
                elif not app._enrollments:
                    armed_until = app._auth.clock() + ARM_S
                    print(
                        f"Enrollment armed for {ARM_S} seconds. Now click Request owner enrollment once in the browser."
                    )
                else:
                    print("One browser enrollment request is required.")
            elif command == "revoke":
                if revocation_approved is not True:
                    print("Revocation is not approved.")
                else:
                    try:
                        app.revoke_owner(enabled=True)
                    except RefreshRejected as exc:
                        # Only the console-confirmation refusal is reported and survivable: revoke_owner raises it
                        # before any state change. Storage/other failures still end the run (fail closed).
                        if exc.code not in REVOCATION_REFUSALS:
                            raise
                        print(f"{REVOCATION_REFUSALS[exc.code]} [{exc.code}]")
                    else:
                        print("Owner credential permanently revoked.")
                        server.should_exit = True
            elif command:
                print("Unknown command. Type enroll, revoke or stop.")
            if command is not None and not server.should_exit:
                print(COMMAND_PROMPT, end="", flush=True)
            elif armed_until and not server.should_exit:
                now = app._auth.clock()
                if len(app._enrollments) == 1:
                    context = next(iter(app._enrollments.values()))
                    if now - (context.expires - ENROLLMENT_S) >= ARM_RESPONSE_S:
                        armed_until = 0.0
                        buffer.clear()
                        print("\nBrowser enrollment request received.")
                        approve_current()
                        print(COMMAND_PROMPT, end="", flush=True)
                elif now >= armed_until:
                    armed_until = 0.0
                    print("\nEnrollment arming expired; type enroll again when ready.")
                    print(COMMAND_PROMPT, end="", flush=True)
            await asyncio.sleep(0.1)
    finally:
        server.should_exit = True
        try:
            await task
        finally:
            app._enrollments.clear()
            app._auth._pending.clear()
            app._auth._grants.clear()
            app._store.close()
            if watchdog is not None:
                watchdog.cancel()  # only after an orderly close; a hung shutdown is still ended by the timer
    return limit_reached
