"""Signing keys and the public JWK Set for Epic Backend Services OAuth (non-production only).

* The RSA private key is generated locally and written outside the repository as
  PKCS#8 PEM, optionally passphrase-encrypted. It is never printed or logged.
* The public JWK Set holds only ``kty``, ``kid``, ``use``, ``alg``, ``n`` and ``e``.
  ``kid`` is the RFC 7638 thumbprint, so it is unique and bound to the key.
* ``check_published_jwks`` fetches the published JWK Set (an unauthenticated GET to
  the static host, not to Epic) and checks that it is safe and matches the local key.

Nothing in this module contacts Epic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.interop.epic import settings

SIGNING_ALG = "RS384"
ALLOWED_KEY_SIZES = (2048, 3072, 4096)
MIN_MODULUS_BITS = 2048
PUBLIC_MEMBERS = frozenset({"kty", "kid", "use", "alg", "n", "e"})
PRIVATE_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
MAX_JWKS_KEYS = 2  # active key plus at most one rotation partner
MAX_JWKS_BYTES = 16 * 1024


class EpicKeyError(RuntimeError):
    """Key or JWK Set problem. Messages never contain key material."""


# --------------------------------------------------------------------------- encoding
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_uint(value: int) -> str:
    return _b64url(value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def _b64url_to_int(text: str) -> int:
    padded = text + "=" * (-len(text) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(padded.encode("ascii")), "big")


def jwk_thumbprint(n: str, e: str) -> str:
    """RFC 7638 SHA-256 thumbprint of an RSA public JWK."""
    canonical = json.dumps({"e": e, "kty": "RSA", "n": n}, separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode("utf-8")).digest())


# --------------------------------------------------------------------------- keys
def generate_private_key(bits: int = 2048) -> rsa.RSAPrivateKey:
    if bits not in ALLOWED_KEY_SIZES:
        raise ValueError(f"key size must be one of {ALLOWED_KEY_SIZES}")
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def public_jwk(key: rsa.RSAPrivateKey | rsa.RSAPublicKey) -> dict[str, str]:
    public = key.public_key() if isinstance(key, rsa.RSAPrivateKey) else key
    numbers = public.public_numbers()
    n, e = _b64url_uint(numbers.n), _b64url_uint(numbers.e)
    return {"kty": "RSA", "kid": jwk_thumbprint(n, e), "use": "sig", "alg": SIGNING_ALG, "n": n, "e": e}


def key_id(key: rsa.RSAPrivateKey | rsa.RSAPublicKey) -> str:
    return public_jwk(key)["kid"]


def key_matches_jwk(key: rsa.RSAPrivateKey, jwk: dict[str, Any]) -> bool:
    expected = public_jwk(key)
    return jwk.get("n") == expected["n"] and jwk.get("e") == expected["e"]


def write_private_key(key: rsa.RSAPrivateKey, path: str | Path, passphrase: bytes | None) -> Path:
    """Write the private key as PKCS#8 PEM outside the repository; never overwrite."""
    target = settings.require_outside_repo(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase) if passphrase else serialization.NoEncryption()
    )
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        raise EpicKeyError("refusing to overwrite an existing key file") from None
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)
    return target


def load_private_key(path: str | Path, passphrase: bytes | None) -> rsa.RSAPrivateKey:
    """Load an RSA private key from outside the repository."""
    source = settings.require_outside_repo(path)
    try:
        data = source.read_bytes()
    except OSError:
        raise EpicKeyError("the private key file could not be read") from None
    try:
        key = serialization.load_pem_private_key(data, password=passphrase)
    except TypeError:
        raise EpicKeyError(
            f"the key's encryption does not match the passphrase setting ({settings.PASSPHRASE_ENV})"
        ) from None
    except ValueError:
        raise EpicKeyError("the private key could not be decrypted or parsed") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise EpicKeyError("the signing key must be an RSA private key")
    if key.key_size < MIN_MODULUS_BITS:
        raise EpicKeyError(f"the signing key must be at least {MIN_MODULUS_BITS} bits")
    return key


def key_file_permission_advice(path: str | Path) -> str | None:
    """Best-effort advice when the key file may be readable by other accounts."""
    target = Path(path)
    if os.name == "nt":
        # Name the account explicitly: %USERNAME% is not expanded by PowerShell. Full control (not read-only)
        # keeps the owner able to delete the key during rotation.
        user = os.environ.get("USERNAME") or "<your Windows account>"
        return f'restrict the key file to your account: icacls "{target}" /inheritance:r /grant:r "{user}:F"'
    if target.stat().st_mode & 0o077:
        return f'the key file is readable by other users: chmod 600 "{target}"'
    return None


# --------------------------------------------------------------------------- JWK Set
def lint_jwks(document: Any) -> list[str]:
    """Return problems that make a JWK Set unsafe or unusable; empty means publishable."""
    if not isinstance(document, dict):
        return ["the JWK Set must be a JSON object"]
    issues: list[str] = []
    if set(document) - {"keys"}:
        issues.append("unexpected top-level members in the JWK Set")
    keys = document.get("keys")
    if not isinstance(keys, list) or not keys:
        return issues + ["the JWK Set must contain a non-empty 'keys' array"]
    if len(keys) > MAX_JWKS_KEYS:
        issues.append(f"the JWK Set holds {len(keys)} keys; at most {MAX_JWKS_KEYS} are allowed")
    seen: set[str] = set()
    for index, jwk in enumerate(keys):
        label = f"keys[{index}]"
        if not isinstance(jwk, dict):
            issues.append(f"{label} is not an object")
            continue
        private = sorted(PRIVATE_MEMBERS & set(jwk))
        if private:
            issues.append(f"{label} contains private key members: {', '.join(private)}")
        unexpected = sorted(set(jwk) - PUBLIC_MEMBERS - PRIVATE_MEMBERS)
        if unexpected:
            issues.append(f"{label} has unexpected members: {', '.join(unexpected)}")
        if jwk.get("kty") != "RSA":
            issues.append(f"{label} kty must be RSA")
        if jwk.get("use") != "sig":
            issues.append(f"{label} use must be sig")
        if jwk.get("alg") != SIGNING_ALG:
            issues.append(f"{label} alg must be {SIGNING_ALG}")
        n, e, kid = jwk.get("n"), jwk.get("e"), jwk.get("kid")
        if not isinstance(n, str) or not isinstance(e, str):
            issues.append(f"{label} must have string n and e")
            continue
        try:
            modulus, exponent = _b64url_to_int(n), _b64url_to_int(e)
        except (ValueError, UnicodeEncodeError):
            issues.append(f"{label} n or e is not valid base64url")
            continue
        if modulus.bit_length() < MIN_MODULUS_BITS:
            issues.append(f"{label} modulus is shorter than {MIN_MODULUS_BITS} bits")
        if exponent != 65537:
            issues.append(f"{label} public exponent must be 65537")
        if not isinstance(kid, str) or kid != jwk_thumbprint(n, e):
            issues.append(f"{label} kid must be the RFC 7638 thumbprint of the key")
        elif kid in seen:
            issues.append(f"{label} duplicates kid {kid}")
        else:
            seen.add(kid)
    return issues


def build_jwks(public_jwks: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble a JWK Set from public JWKs and refuse anything ``lint_jwks`` rejects."""
    document = {
        "keys": [
            {name: jwk[name] for name in ("kty", "kid", "use", "alg", "n", "e") if name in jwk} for jwk in public_jwks
        ]
    }
    leaked = [jwk for jwk in public_jwks if PRIVATE_MEMBERS & set(jwk)]
    issues = lint_jwks(document) + (["an input key contains private members"] if leaked else [])
    if issues:
        raise EpicKeyError("; ".join(issues))
    return document


def write_jwks(document: dict[str, Any], path: str | Path) -> Path:
    issues = lint_jwks(document)
    if issues:
        raise EpicKeyError("; ".join(issues))
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return target


def read_jwks(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    if len(raw) > MAX_JWKS_BYTES:
        raise EpicKeyError("the JWK Set file is larger than expected")
    try:
        document = json.loads(raw)
    except ValueError:
        raise EpicKeyError("the JWK Set file is not valid JSON") from None
    if not isinstance(document, dict):
        raise EpicKeyError("the JWK Set must be a JSON object")
    return document


# --------------------------------------------------------------------------- published check
@dataclass
class JwksCheckReport:
    url: str
    ok: bool = False
    status_code: int | None = None
    content_type: str | None = None
    cache_control: str | None = None
    age: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    kids: list[str] = field(default_factory=list)
    expected_kid: str | None = None
    expected_kid_present: bool | None = None
    expected_key_matches: bool | None = None
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_published_jwks(
    url: str,
    *,
    private_key: rsa.RSAPrivateKey | None = None,
    expected_kid: str | None = None,
    timeout_s: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> JwksCheckReport:
    """Fetch the published JWK Set and verify it is public-only and matches the local key."""
    if private_key is not None:
        expected_kid = key_id(private_key)
    report = JwksCheckReport(url=url, expected_kid=expected_kid)
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        report.issues.append("the JWK Set URL must be a plain https URL")
        return report
    body = bytearray()
    try:
        with httpx.Client(
            timeout=timeout_s,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": settings.USER_AGENT},
        ) as client:
            with client.stream("GET", url) as response:
                report.status_code = response.status_code
                headers = response.headers
                report.content_type = headers.get("content-type")
                report.cache_control = headers.get("cache-control")
                report.age = headers.get("age")
                report.etag = headers.get("etag")
                report.last_modified = headers.get("last-modified")
                if response.status_code != 200:
                    location = urlsplit(headers.get("location", "")).hostname
                    detail = f" (redirect to {location} not followed)" if 300 <= response.status_code < 400 else ""
                    report.issues.append(f"expected HTTP 200, got {response.status_code}{detail}")
                    return report
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_JWKS_BYTES:
                        report.issues.append("the JWK Set response is larger than expected")
                        return report
    except httpx.HTTPError as exc:
        report.issues.append(f"the JWK Set URL could not be fetched ({type(exc).__name__})")
        return report
    if "json" not in (report.content_type or "").lower():
        report.issues.append("the JWK Set must be served with a JSON content type")
    try:
        document = json.loads(bytes(body))
    except ValueError:
        report.issues.append("the JWK Set response is not valid JSON")
        return report
    report.issues.extend(lint_jwks(document))
    keys = document.get("keys") if isinstance(document, dict) else None
    published = [jwk for jwk in keys if isinstance(jwk, dict)] if isinstance(keys, list) else []
    report.kids = [str(jwk.get("kid")) for jwk in published]
    if expected_kid is not None:
        match = next((jwk for jwk in published if jwk.get("kid") == expected_kid), None)
        report.expected_kid_present = match is not None
        if match is None:
            report.issues.append("the active key id is not published")
        elif private_key is not None:
            report.expected_key_matches = key_matches_jwk(private_key, match)
            if not report.expected_key_matches:
                report.issues.append("the published key does not match the local private key")
    report.ok = not report.issues
    return report
