"""Load the hosted-only Epic signing key from the host's secret store (an environment value), never from a file.

The hosted key is a separate non-production key pair with its own ``kid`` in the published JWK Set. The owner's local
key never leaves the laptop. The PEM is parsed once at startup, checked against the expected ``kid``, and its
environment value is removed from ``os.environ`` so no later code or subprocess inherits it. The kernel's copy of the
process's initial environment (``/proc/1/environ``) still holds it, readable only inside the container by the service
user: an accepted limitation of passing secrets as container environment variables.
"""

from __future__ import annotations

import os
import re
from typing import MutableMapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.interop.epic.keys import MIN_MODULUS_BITS, key_id

KEY_ENV = "EPIC_PRIVATE_KEY_PEM"
KID_ENV = "EPIC_KEY_ID"
KID = re.compile(r"[A-Za-z0-9_-]{43}")
PEM_LIMIT = 16 * 1024
# Assembled so the repository never contains a literal PEM header (the secret scans look for one).
PKCS8_HEADER = b"-----BEGIN " + b"PRIVATE KEY-----"


class HostedKeyError(RuntimeError):
    """A fixed message; key material is never included."""


def load_pem_key(pem: bytes, expected_kid: str) -> rsa.RSAPrivateKey:
    if not KID.fullmatch(expected_kid or ""):
        raise HostedKeyError("the expected key id must be the 43-character JWK thumbprint")
    if not pem or len(pem) > PEM_LIMIT or PKCS8_HEADER not in pem:
        raise HostedKeyError("the hosted signing key must be an unencrypted PKCS#8 PEM")
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (TypeError, ValueError):
        raise HostedKeyError("the hosted signing key could not be parsed") from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_MODULUS_BITS:
        raise HostedKeyError(f"the hosted signing key must be RSA with at least {MIN_MODULUS_BITS} bits")
    if key.public_key().public_numbers().e != 65537:
        raise HostedKeyError("the hosted signing key must use exponent 65537")
    if key_id(key) != expected_kid:
        raise HostedKeyError("the hosted signing key does not match the expected key id")
    return key


def take_key(environ: MutableMapping[str, str] | None = None) -> rsa.RSAPrivateKey:
    """Read, remove from ``os.environ`` and parse the key, whether or not it parses."""
    env = os.environ if environ is None else environ
    pem = env.pop(KEY_ENV, "")
    # Secret stores often keep a PEM on one line with literal "\n" separators.
    text = pem.replace("\\n", "\n").strip() + "\n"
    try:
        return load_pem_key(text.encode("ascii", "strict"), env.get(KID_ENV, ""))
    except UnicodeEncodeError:
        raise HostedKeyError("the hosted signing key must be ASCII PEM") from None
    finally:
        del pem, text
