"""Epic Backend Services token client and local configuration (MOCK/offline only).

Every HTTP exchange is served by ``httpx.MockTransport``; nothing here contacts
Epic. Signing keys are ephemeral, in-memory test keys.
"""

from __future__ import annotations

import base64
import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from src.clinical.fhir_client import FHIRClientError
from src.interop.epic import auth, keys, settings

CLIENT_ID = "0000aaaa-test-client-id-not-real"
TOKEN_VALUE = "unit-test-access-token-value-not-real"


@pytest.fixture(scope="module")
def private_key():
    return keys.generate_private_key(2048)


def _decode(segment: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _token_server(responses: list, seen: list | None = None):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler)


def _ok(expires_in: int = 3600, scope: str = "Patient.read Patient.search Observation.search") -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": TOKEN_VALUE, "token_type": "bearer", "expires_in": expires_in, "scope": scope}
    )


# --------------------------------------------------------------------------- client assertion
def test_client_assertion_claims_header_and_signature(private_key):
    assertion = auth.build_client_assertion(private_key, CLIENT_ID, now=1_800_000_000)
    header_b64, claims_b64, signature_b64 = assertion.split(".")
    header, claims = _decode(header_b64), _decode(claims_b64)
    assert header == {"alg": "RS384", "typ": "JWT", "kid": keys.key_id(private_key)}
    assert claims["iss"] == claims["sub"] == CLIENT_ID
    assert claims["aud"] == settings.TOKEN_URL == "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"
    assert claims["iat"] == claims["nbf"] == 1_800_000_000
    assert 0 < claims["exp"] - claims["iat"] <= 300
    assert 0 < len(claims["jti"]) <= 151
    signature = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    private_key.public_key().verify(
        signature, f"{header_b64}.{claims_b64}".encode(), padding.PKCS1v15(), hashes.SHA384()
    )


def test_client_assertion_jti_is_unique_and_lifetime_is_bounded(private_key):
    first = _decode(auth.build_client_assertion(private_key, CLIENT_ID).split(".")[1])["jti"]
    second = _decode(auth.build_client_assertion(private_key, CLIENT_ID).split(".")[1])["jti"]
    assert first != second
    with pytest.raises(ValueError):
        auth.build_client_assertion(private_key, CLIENT_ID, lifetime_s=301)
    with_jku = auth.build_client_assertion(private_key, CLIENT_ID, jku="https://jwks.example.test/jwks.json")
    assert _decode(with_jku.split(".")[0])["jku"] == "https://jwks.example.test/jwks.json"


# --------------------------------------------------------------------------- token requests
def test_token_request_is_a_form_post_to_the_pinned_endpoint(private_key):
    seen: list[httpx.Request] = []
    provider = auth.EpicTokenProvider(CLIENT_ID, private_key, transport=_token_server([_ok()], seen))
    token = provider.token()
    assert token.scopes == ("Observation.search", "Patient.read", "Patient.search")
    request = seen[0]
    assert request.method == "POST" and str(request.url) == settings.TOKEN_URL
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert "authorization" not in request.headers
    form = parse_qs(request.content.decode())
    assert set(form) == {"grant_type", "client_assertion_type", "client_assertion"}
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_assertion_type"] == ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"]
    provider.close()


def test_token_is_cached_and_refreshed_before_expiry(private_key):
    seen: list[httpx.Request] = []
    clock = FakeClock()
    provider = auth.EpicTokenProvider(
        CLIENT_ID, private_key, transport=_token_server([_ok(300)], seen), clock=clock, refresh_skew_s=60
    )
    provider.token()
    clock.now += 200
    provider.token()
    assert len(seen) == 1
    clock.now += 50  # within the 60 s refresh window
    provider.token()
    assert len(seen) == 2
    provider.invalidate()
    provider.token()
    assert len(seen) == 3


@pytest.mark.parametrize(
    "response, category",
    [
        (
            httpx.Response(400, json={"error": "invalid_client", "error_description": "private diagnostic detail"}),
            "invalid_client",
        ),
        (httpx.Response(400, json={"error": "invalid_scope"}), "invalid_scope"),
        (httpx.Response(401, json={"error": "something_else"}), "http_4xx"),
        (httpx.Response(400, text="not json"), "http_4xx"),
        (httpx.Response(302, headers={"location": "https://elsewhere.example/token"}), "redirect_refused"),
        (httpx.Response(429), "throttled"),
        (httpx.Response(503), "http_5xx"),
        (httpx.Response(200, json={"access_token": TOKEN_VALUE, "expires_in": 3600}), "malformed"),
        (httpx.Response(200, json={"access_token": "", "token_type": "bearer", "expires_in": 3600}), "malformed"),
        (httpx.Response(200, json={"access_token": TOKEN_VALUE, "token_type": "bearer", "expires_in": 0}), "malformed"),
        (httpx.Response(200, text="<html>"), "malformed"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "network"),
    ],
)
def test_token_failures_are_categorised_without_leaking(private_key, caplog, response, category):
    seen: list[httpx.Request] = []
    provider = auth.EpicTokenProvider(CLIENT_ID, private_key, transport=_token_server([response], seen))
    with caplog.at_level(logging.DEBUG), pytest.raises(auth.EpicAuthError) as info:
        provider.token()
    assert info.value.category == category
    assert isinstance(info.value, FHIRClientError)
    assertion = parse_qs(seen[0].content.decode())["client_assertion"][0]
    for text in (str(info.value), repr(info.value), caplog.text, repr(provider)):
        assert TOKEN_VALUE not in text and assertion not in text and "private diagnostic" not in text
    assert len(seen) == 1  # no automatic retries or redirect following for token requests
    counters = provider.telemetry.describe()["counters"]
    assert counters == {f"token.error.{category}": 1}


def test_token_value_never_appears_in_reprs_or_telemetry(private_key):
    provider = auth.EpicTokenProvider(CLIENT_ID, private_key, transport=_token_server([_ok()]))
    token = provider.token()
    assert token.value == TOKEN_VALUE
    rendered = repr(token) + repr(provider) + json.dumps(provider.telemetry.describe())
    assert TOKEN_VALUE not in rendered and CLIENT_ID not in rendered
    assert provider.telemetry.describe()["counters"] == {"token.ok": 1}


# --------------------------------------------------------------------------- local configuration
@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(settings, "REPO_ROOT", repo)
    return repo


def test_config_reads_file_outside_repo_and_env_overrides(tmp_path, fake_repo):
    env_file = tmp_path / "epic.env"
    env_file.write_text(
        f"EPIC_SANDBOX_CLIENT_ID={CLIENT_ID}\nEPIC_SANDBOX_PRIVATE_KEY_PATH={tmp_path / 'k.pem'}\n", encoding="utf-8"
    )
    config = settings.load_config({"EPIC_SANDBOX_ENV_FILE": str(env_file)})
    assert config.client_id == CLIENT_ID and config.private_key_path == (tmp_path / "k.pem").resolve()
    assert CLIENT_ID not in repr(config) and config.client_hint == "...real"
    override = settings.load_config(
        {"EPIC_SANDBOX_ENV_FILE": str(env_file), "EPIC_SANDBOX_JKU": "https://jwks.example.test/jwks.json"}
    )
    assert override.jku == "https://jwks.example.test/jwks.json"


def test_config_refuses_passphrase_in_file_keys_in_repo_and_bad_client_ids(tmp_path, fake_repo):
    env_file = tmp_path / "epic.env"
    env_file.write_text(f"EPIC_SANDBOX_CLIENT_ID={CLIENT_ID}\nEPIC_SANDBOX_KEY_PASSPHRASE=x\n", encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="out of the config file"):
        settings.load_config({"EPIC_SANDBOX_ENV_FILE": str(env_file)})
    missing = tmp_path / "missing.env"
    with pytest.raises(settings.LocalPathError):
        settings.load_config(
            {
                "EPIC_SANDBOX_ENV_FILE": str(missing),
                "EPIC_SANDBOX_CLIENT_ID": CLIENT_ID,
                "EPIC_SANDBOX_PRIVATE_KEY_PATH": str(fake_repo / "k.pem"),
            }
        )
    with pytest.raises(settings.ConfigError, match="client ID"):
        settings.load_config(
            {
                "EPIC_SANDBOX_ENV_FILE": str(missing),
                "EPIC_SANDBOX_CLIENT_ID": "bad id!",
                "EPIC_SANDBOX_PRIVATE_KEY_PATH": str(tmp_path / "k.pem"),
            }
        )


def test_live_calls_require_explicit_opt_in():
    with pytest.raises(settings.SandboxDisabled):
        settings.require_enabled({})
    with pytest.raises(settings.SandboxDisabled):
        settings.require_enabled({"EPIC_SANDBOX_ENABLED": "true"})
    settings.require_enabled({"EPIC_SANDBOX_ENABLED": "1"})
