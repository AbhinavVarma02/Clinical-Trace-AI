"""Block accidental external Python socket I/O in offline test subprocesses.

Numeric loopback and Unix-domain IPC remain available for asyncio on Windows/Linux.
Install before importing application or HTTP-client code. This is a test guard,
not a security sandbox for hostile code or native libraries.
"""

from __future__ import annotations

import ipaddress
import socket
import sys

blocked_events: list[str] = []
_installed = False
_PROBE = "clinical_trace.offline_guard_probe"


class NetworkBlocked(OSError):
    """An offline test attempted prohibited socket I/O or name resolution."""


def _loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or (
        isinstance(address, ipaddress.IPv6Address)
        and address.ipv4_mapped is not None
        and address.ipv4_mapped.is_loopback
    )


def _audit(event: str, args: tuple[object, ...]) -> None:
    if event == _PROBE:
        raise NetworkBlocked("offline guard installed")
    if event in {"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"}:
        sock, address = args
        if isinstance(sock, socket.socket) and sock.family == getattr(socket, "AF_UNIX", None):
            return
        if isinstance(address, tuple) and address and _loopback(address[0]):
            return
    elif event in {"socket.getaddrinfo", "socket.gethostbyname"}:
        if args and _loopback(args[0]):
            return
    elif event not in {"socket.gethostbyaddr", "socket.getnameinfo"}:
        return
    blocked_events.append(event)
    raise NetworkBlocked(f"offline test blocked {event}")


def install() -> None:
    """Install once and prove registration succeeded, or abort before tests run."""
    global _installed
    if not _installed:
        sys.addaudithook(_audit)
    try:
        sys.audit(_PROBE)
    except NetworkBlocked:
        _installed = True
    else:
        raise RuntimeError("offline network guard could not be installed")
