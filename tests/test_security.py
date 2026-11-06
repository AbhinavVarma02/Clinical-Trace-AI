"""Security posture tests."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from src.security import contains_secret_pattern, scan_file_for_secrets

PEM = serialization.Encoding.PEM
NO_ENCRYPTION = serialization.NoEncryption()


def test_gitignore_contains_env():
    content = Path(".gitignore").read_text(encoding="utf-8")
    assert ".env" in content.splitlines()


def test_env_example_contains_placeholders_only():
    content = Path(".env.example").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=your_openai_api_key_here" in content
    assert "LANGCHAIN_API_KEY=your_langsmith_api_key_here" in content


def test_source_files_do_not_contain_common_secret_patterns():
    roots = [Path("src"), Path("api"), Path("dashboard"), Path("tests"), Path("prompts"), Path("docs")]
    scanned = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".txt", ".md"}:
                scanned.append(path)
                assert not scan_file_for_secrets(path), f"Secret-like pattern found in {path}"
    assert scanned


# Private keys and tokens are generated at run time: a literal one in this file would fail the repository scan.
@pytest.fixture(scope="module")
def keys():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048), ec.generate_private_key(ec.SECP256R1())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signed_jwt() -> str:
    header = {"alg": "RS384", "typ": "JWT", "kid": "test-only"}
    claims = {"iss": "test-client", "sub": "test-client", "aud": "https://token.example.invalid", "exp": 1}
    return ".".join([_b64url(json.dumps(header).encode()), _b64url(json.dumps(claims).encode()),
                     _b64url(os.urandom(256))])


def test_pem_private_keys_are_detected_in_every_common_format(keys):
    rsa_key, ec_key = keys
    formats = {
        "PKCS#8": rsa_key.private_bytes(PEM, serialization.PrivateFormat.PKCS8, NO_ENCRYPTION),
        "encrypted PKCS#8 (the Epic sandbox key's format)": rsa_key.private_bytes(
            PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"test-only-passphrase")),
        "PKCS#1 RSA": rsa_key.private_bytes(PEM, serialization.PrivateFormat.TraditionalOpenSSL, NO_ENCRYPTION),
        "SEC1 EC": ec_key.private_bytes(PEM, serialization.PrivateFormat.TraditionalOpenSSL, NO_ENCRYPTION),
        "OpenSSH": ec_key.private_bytes(PEM, serialization.PrivateFormat.OpenSSH, NO_ENCRYPTION),
    }
    for name, pem in formats.items():
        assert contains_secret_pattern(pem.decode()), name


def test_a_signed_jwt_shaped_token_is_detected():
    token = _signed_jwt()
    assert contains_secret_pattern(token)
    assert contains_secret_pattern(f"Authorization: Bearer {token}\n")
    assert contains_secret_pattern(json.dumps({"access_token": token, "expires_in": 3600}))


def test_public_material_and_prose_are_not_flagged(keys):
    rsa_key, _ = keys
    public_pem = rsa_key.public_key().public_bytes(PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    numbers = rsa_key.public_key().public_numbers()
    jwks = {"keys": [{"kty": "RSA", "alg": "RS384", "use": "sig", "kid": "test-only",
                      "n": _b64url(numbers.n.to_bytes(256, "big")), "e": _b64url(numbers.e.to_bytes(3, "big"))}]}
    header_only = _signed_jwt().split(".")[0]
    for text in (public_pem, json.dumps(jwks), header_only, "Never commit a PRIVATE KEY or a token.",
                 "version 1.2.3 of the client"):
        assert not contains_secret_pattern(text), text


def test_a_file_holding_a_private_key_or_token_is_flagged(keys, tmp_path):
    rsa_key, _ = keys
    pem = tmp_path / "leaked-key.txt"
    pem.write_bytes(rsa_key.private_bytes(PEM, serialization.PrivateFormat.PKCS8, NO_ENCRYPTION))
    token = tmp_path / "notes.md"
    token.write_text(f"debug output: {_signed_jwt()}\n", encoding="utf-8")
    clean = tmp_path / "clean.md"
    clean.write_text("Keys stay outside the repository.\n", encoding="utf-8")
    assert scan_file_for_secrets(pem) and scan_file_for_secrets(token) and not scan_file_for_secrets(clean)
