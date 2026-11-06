"""Epic sandbox signing keys and JWK Set tooling (MOCK/offline: no Epic or network calls).

Keys are ephemeral test keys generated in memory or under a temporary directory.
No key material is written to the repository and no PEM text appears in this file.
"""

from __future__ import annotations

import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.interop.epic import cli, keys, settings
from src.security import contains_secret_pattern

JWKS_URL = "https://jwks.example.test/clinical-trace/jwks.json"


@pytest.fixture(scope="module")
def key_pair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    return keys.generate_private_key(2048), keys.generate_private_key(2048)


@pytest.fixture
def outside_repo(tmp_path, monkeypatch):
    """Treat a sibling temp directory as the repository so tmp_path counts as 'outside'."""
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    monkeypatch.setattr(settings, "REPO_ROOT", fake_repo)
    home = tmp_path / "home"
    home.mkdir()
    return home, fake_repo


def _serve(
    document: object,
    *,
    status: int = 200,
    content_type: str = "application/json",
    headers: dict[str, str] | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "authorization" not in request.headers
        return httpx.Response(
            status, content=json.dumps(document).encode(), headers={"content-type": content_type, **(headers or {})}
        )

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- JWK basics
def test_public_jwk_has_only_public_members_and_rfc7638_kid(key_pair):
    jwk = keys.public_jwk(key_pair[0])
    assert set(jwk) == {"kty", "kid", "use", "alg", "n", "e"}
    assert jwk["alg"] == "RS384" and jwk["use"] == "sig" and jwk["kty"] == "RSA"
    assert jwk["kid"] == keys.jwk_thumbprint(jwk["n"], jwk["e"])
    assert keys.key_id(key_pair[0]) == jwk["kid"]
    assert keys.lint_jwks({"keys": [jwk]}) == []


def test_thumbprint_matches_rfc7638_example():
    n = (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMs"
        "tn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5h"
        "ajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
    )
    assert keys.jwk_thumbprint(n, "AQAB") == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_two_key_rotation_set_is_publishable(key_pair):
    document = keys.build_jwks([keys.public_jwk(key_pair[0]), keys.public_jwk(key_pair[1])])
    assert keys.lint_jwks(document) == []
    assert len({jwk["kid"] for jwk in document["keys"]}) == 2


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda jwk: jwk.update(d="AQAB"), "private key members: d"),
        (lambda jwk: jwk.update(alg="RS256"), "alg must be RS384"),
        (lambda jwk: jwk.update(use="enc"), "use must be sig"),
        (lambda jwk: jwk.update(kty="EC"), "kty must be RSA"),
        (lambda jwk: jwk.update(kid="custom-id"), "RFC 7638 thumbprint"),
        (lambda jwk: jwk.update(x5c=["cert"]), "unexpected members: x5c"),
        (lambda jwk: jwk.update(e="Aw"), "exponent must be 65537"),
    ],
)
def test_lint_rejects_unsafe_or_wrong_keys(key_pair, mutate, message):
    jwk = keys.public_jwk(key_pair[0])
    mutate(jwk)
    issues = keys.lint_jwks({"keys": [jwk]})
    assert any(message in issue for issue in issues), issues


def test_lint_rejects_duplicates_too_many_keys_and_short_modulus(key_pair):
    jwk = keys.public_jwk(key_pair[0])
    assert any("duplicates kid" in i for i in keys.lint_jwks({"keys": [jwk, dict(jwk)]}))
    third = keys.public_jwk(keys.generate_private_key(2048))
    assert any("at most 2" in i for i in keys.lint_jwks({"keys": [jwk, keys.public_jwk(key_pair[1]), third]}))
    short = keys.public_jwk(rsa.generate_private_key(public_exponent=65537, key_size=1024))
    assert any("shorter than 2048" in i for i in keys.lint_jwks({"keys": [short]}))
    assert keys.lint_jwks([]) == ["the JWK Set must be a JSON object"]
    assert "non-empty 'keys' array" in keys.lint_jwks({"keys": []})[0]


def test_build_jwks_refuses_private_input(key_pair):
    jwk = {**keys.public_jwk(key_pair[0]), "d": "AQAB"}
    with pytest.raises(keys.EpicKeyError, match="private"):
        keys.build_jwks([jwk])


# --------------------------------------------------------------------------- private key files
def test_private_key_is_refused_inside_the_repository(key_pair):
    target = settings.REPO_ROOT / "var" / "never-written.pem"
    with pytest.raises(settings.LocalPathError):
        keys.write_private_key(key_pair[0], target, None)
    assert not target.exists()
    with pytest.raises(settings.LocalPathError):
        keys.load_private_key(settings.REPO_ROOT / "README.md", None)


def test_encrypted_key_round_trip_and_wrong_passphrase(key_pair, outside_repo):
    home, _ = outside_repo
    path = keys.write_private_key(key_pair[0], home / "k.pem", b"correct horse battery")
    assert keys.key_matches_jwk(keys.load_private_key(path, b"correct horse battery"), keys.public_jwk(key_pair[0]))
    with pytest.raises(keys.EpicKeyError) as missing:
        keys.load_private_key(path, None)
    assert settings.PASSPHRASE_ENV in str(missing.value)
    with pytest.raises(keys.EpicKeyError):
        keys.load_private_key(path, b"wrong passphrase")


def test_existing_key_is_never_overwritten(key_pair, outside_repo):
    home, _ = outside_repo
    path = keys.write_private_key(key_pair[0], home / "k.pem", None)
    before = path.read_bytes()
    with pytest.raises(keys.EpicKeyError, match="overwrite"):
        keys.write_private_key(key_pair[1], path, None)
    assert path.read_bytes() == before


# --------------------------------------------------------------------------- published JWK Set check
def test_published_check_passes_and_records_cache_headers(key_pair):
    document = keys.build_jwks([keys.public_jwk(key_pair[0]), keys.public_jwk(key_pair[1])])
    transport = _serve(document, headers={"cache-control": "max-age=600", "etag": '"abc"', "age": "12"})
    report = keys.check_published_jwks(JWKS_URL, private_key=key_pair[1], transport=transport)
    assert report.ok, report.issues
    assert report.expected_kid_present and report.expected_key_matches
    assert report.cache_control == "max-age=600" and report.age == "12" and report.etag == '"abc"'
    assert report.kids == [jwk["kid"] for jwk in document["keys"]]


def test_published_check_fails_when_active_key_missing_or_private_members_leak(key_pair):
    only_first = keys.build_jwks([keys.public_jwk(key_pair[0])])
    report = keys.check_published_jwks(JWKS_URL, private_key=key_pair[1], transport=_serve(only_first))
    assert not report.ok and report.expected_kid_present is False
    leaked = {"keys": [{**keys.public_jwk(key_pair[0]), "d": "AQAB"}]}
    report = keys.check_published_jwks(JWKS_URL, expected_kid=keys.key_id(key_pair[0]), transport=_serve(leaked))
    assert not report.ok and any("private key members" in i for i in report.issues)


def test_published_check_does_not_follow_redirects_or_accept_html(key_pair):
    document = keys.build_jwks([keys.public_jwk(key_pair[0])])
    redirect = _serve({}, status=302, headers={"location": "https://elsewhere.example/jwks.json"})
    report = keys.check_published_jwks(JWKS_URL, transport=redirect)
    assert not report.ok and "redirect to elsewhere.example not followed" in report.issues[0]
    html = keys.check_published_jwks(JWKS_URL, transport=_serve(document, content_type="text/html"))
    assert not html.ok and any("JSON content type" in i for i in html.issues)


def test_published_check_refuses_non_https_and_oversized_bodies():
    def never(_request):
        pytest.fail("no request may be sent for an insecure URL")

    report = keys.check_published_jwks("http://jwks.example.test/jwks.json", transport=httpx.MockTransport(never))
    assert not report.ok and "https" in report.issues[0]
    big = httpx.MockTransport(
        lambda _r: httpx.Response(
            200, content=b"x" * (keys.MAX_JWKS_BYTES + 1), headers={"content-type": "application/json"}
        )
    )
    assert "larger than expected" in keys.check_published_jwks(JWKS_URL, transport=big).issues[0]


# --------------------------------------------------------------------------- CLI
def test_cli_generate_writes_outside_repo_and_prints_no_key_material(outside_repo, capsys, monkeypatch):
    home, _ = outside_repo
    monkeypatch.delenv(settings.PASSPHRASE_ENV, raising=False)
    assert cli.main(["keys", "generate", "--out-dir", str(home / "keys"), "--no-passphrase"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert not contains_secret_pattern(output)
    assert "PRIVATE" not in output and '"d"' not in output
    key_file = home / "keys" / f"{result['kid']}.pem"
    jwks_file = home / "keys" / f"{result['kid']}.jwks.json"
    assert key_file.exists() and jwks_file.exists()
    published = json.loads(jwks_file.read_text(encoding="utf-8"))
    assert keys.lint_jwks(published) == [] and published["keys"][0]["kid"] == result["kid"]
    assert cli.main(["jwks", "lint", str(jwks_file)]) == 0


def test_cli_generate_uses_passphrase_env_and_refuses_repo_output(outside_repo, capsys, monkeypatch):
    home, fake_repo = outside_repo
    monkeypatch.setenv(settings.PASSPHRASE_ENV, "a long local passphrase")
    assert cli.main(["keys", "generate", "--out-dir", str(home / "k")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["private_key_encrypted"] is True
    assert cli.main(["keys", "generate", "--out-dir", str(fake_repo / "keys")]) == 2
    assert "inside the repository" in json.loads(capsys.readouterr().out)["error"]
    assert not (fake_repo / "keys").exists()


def test_cli_merge_builds_rotation_set(key_pair, outside_repo, capsys):
    home, _ = outside_repo
    first = keys.write_jwks(keys.build_jwks([keys.public_jwk(key_pair[0])]), home / "a.jwks.json")
    second = keys.write_jwks(keys.build_jwks([keys.public_jwk(key_pair[1])]), home / "b.jwks.json")
    assert cli.main(["jwks", "merge", str(first), str(second), "--out", str(home / "jwks.json")]) == 0
    merged = keys.read_jwks(home / "jwks.json")
    assert keys.lint_jwks(merged) == [] and len(merged["keys"]) == 2
