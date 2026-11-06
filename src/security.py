"""Security utilities for secret scanning and synthetic-identifier checks.

Small, dependency-free helpers used by the API and the test suite to enforce two
guardrails: reject patient IDs that are not clearly synthetic, and detect common
secret token patterns so credentials never slip into tracked files.
"""

from __future__ import annotations

import re
from pathlib import Path


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # PEM private keys: PKCS#8 (plain or encrypted), PKCS#1 RSA, SEC1 EC, DSA and OpenSSH. Public keys do not match.
    re.compile(r"-----BEGIN (?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----"),
    # Signed JWT-shaped tokens (base64url JSON header and claims, then a signature), e.g. bearer access tokens.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}"),
]


def contains_secret_pattern(text: str) -> bool:
    """Return True if text contains a common secret token pattern."""
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def scan_file_for_secrets(path: Path) -> bool:
    """Scan a text file for common secret patterns."""
    try:
        return contains_secret_pattern(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return False


def is_synthetic_patient_id(patient_id: str) -> bool:
    """Validate that an identifier is clearly synthetic."""
    return patient_id.startswith("synthetic_")
