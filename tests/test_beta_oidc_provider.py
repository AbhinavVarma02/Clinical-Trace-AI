"""AUTH-2a (offline): pinned provider metadata, discovery and a rotating JWKS key source for the owner relying party.

Every test talks to an in-process OIDC test double through ``httpx.MockTransport`` under the offline socket guard.
This proves the relying party's key and endpoint handling, not a real identity provider: no AUTH-2b login has run,
and real promotion stays disabled.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from api.beta import create_mock_beta_app
from api.beta_oidc import OwnerAuth, OwnerIdentity
from api.beta_oidc_keys import JwksKeys, ProviderError, ProviderMetadata, discover, parse_jwks
from src.interop.epic.beta_catalog import Catalog, RefreshRejected

ISSUER = "https://idp.invalid/pool-1"  # a path on the issuer, as managed-login providers have
AUTHORIZE = "https://login.idp.invalid/oauth2/authorize"  # and endpoints on another host
TOKEN = "https://login.idp.invalid/oauth2/token"
JWKS = f"{ISSUER}/.well-known/jwks.json"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
CLIENT = "invented-client-id"
OWNER = "invented-owner-sub"
ORIGIN = "https://beta.invalid"
IDENTITY = OwnerIdentity(ISSUER, CLIENT, OWNER)
METADATA = ProviderMetadata(ISSUER, AUTHORIZE, TOKEN, JWKS)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(kid, key, **extra):
    numbers = (key.public_key() if hasattr(key, "public_key") else key).public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": b64(numbers.e.to_bytes(3, "big")),
    } | extra


def sign(key, claims, header):
    head = b64(json.dumps(header).encode())
    body = b64(json.dumps(claims).encode())
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(signature)}"


@pytest.fixture(scope="module")
def keys():
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(4))


class Double:
    """An invented OIDC provider: discovery, JWKS and token endpoints, with knobs for every failure."""

    def __init__(self, keys):
        self.clock = time.time()
        self.keys = keys
        self.signer, self.kid = keys[0], "kid-1"
        self.published = {"kid-1": keys[0]}
        self.jwks_override = None  # a callable returning an httpx.Response
        self.discovery = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZE,
            "token_endpoint": TOKEN,
            "jwks_uri": JWKS,
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
        }
        self.claims = {}
        self.header = None  # replaces the whole JWS header when set
        self.token = None  # replaces the whole id_token when set
        self.nonce = ""
        self.urls = []

    def count(self, url):
        return self.urls.count(url)

    def now(self):
        return int(self.clock)

    async def handler(self, request):
        url = str(request.url)
        self.urls.append(url)
        if url == DISCOVERY:
            return httpx.Response(200, json=self.discovery)
        if url == JWKS:
            await asyncio.sleep(0)  # a real fetch yields, so concurrent lookups genuinely interleave
            if self.jwks_override is not None:
                return self.jwks_override()
            return httpx.Response(200, json={"keys": [jwk(kid, key) for kid, key in self.published.items()]})
        assert url == TOKEN and request.method == "POST"
        fields = parse_qs(request.content.decode())
        assert fields["grant_type"] == ["authorization_code"] and fields["client_id"] == [CLIENT]
        assert "client_secret" not in fields  # a public client: PKCE only, no secret to store
        claims = {
            "iss": ISSUER,
            "sub": OWNER,
            "aud": CLIENT,
            "nonce": self.nonce,
            "iat": self.now(),
            "exp": self.now() + 300,
        } | self.claims
        header = self.header if self.header is not None else {"alg": "RS256", "kid": self.kid}
        token = self.token if self.token is not None else sign(self.signer, claims, header)
        return httpx.Response(200, json={"id_token": token})

    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture
def double(keys):
    return Double(keys)


@pytest.fixture
def parts(tmp_path, double):
    transport = double.transport()
    catalog = Catalog(tmp_path / "owner.sqlite", lambda: double.clock)
    jwks = JwksKeys(transport, JWKS, lambda: double.clock)
    auth = OwnerAuth(catalog, None, transport, IDENTITY, provider=METADATA, keys=jwks)
    yield auth, jwks, catalog, transport
    catalog.close()


def login(auth, double, *, code="invented-code"):
    browser, url = auth.begin(None)
    query = parse_qs(urlsplit(url).query)
    double.nonce = query["nonce"][0]
    return asyncio.run(auth.complete(browser, query["state"][0], code)), url


def rejected(auth, double):
    with pytest.raises(RefreshRejected) as raised:
        login(auth, double)
    assert (raised.value.status, raised.value.code) == (401, "login_rejected")


# -- endpoints come from the pinned metadata -----------------------------------------------------------------


def test_authorize_and_token_endpoints_come_from_the_pinned_metadata(parts, double):
    auth, *_ = parts
    channel, url = login(auth, double)
    assert channel == "legacy-json"
    assert url.startswith(f"{AUTHORIZE}?") and "client_id=" + CLIENT in url
    assert double.count(TOKEN) == 1 and double.count(JWKS) == 1
    assert auth.pending_auth.amr == ()  # claims handling is unchanged: no claim, no assurance


def test_step_up_carries_prompt_and_max_age_to_the_provider_endpoint(parts, double):
    auth, *_ = parts
    login(auth, double)
    token, _ = auth.create_session("")
    _, url = auth.begin_step_up(token, None)
    query = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{AUTHORIZE}?")
    assert query["prompt"] == ["login"] and query["max_age"] == ["0"] and query["code_challenge_method"] == ["S256"]


def test_the_key_set_is_fetched_once_and_cached_across_logins(parts, double):
    auth, *_ = parts
    for _ in range(3):
        login(auth, double)
    assert double.count(JWKS) == 1


# -- rotation ------------------------------------------------------------------------------------------------


def test_a_rotated_key_is_picked_up_by_the_first_token_that_names_it(parts, double, keys):
    auth, jwks, *_ = parts
    login(auth, double)
    double.published = {"kid-1": keys[0], "kid-2": keys[1]}  # the provider publishes the next key, then starts using it
    double.clock += 61  # past the refetch throttle
    double.signer, double.kid = keys[1], "kid-2"
    login(auth, double)
    assert set(jwks.keys) == {"kid-1", "kid-2"} and double.count(JWKS) == 2


def test_a_withdrawn_key_stops_working_once_the_set_is_refreshed(parts, double, keys):
    auth, jwks, *_ = parts
    login(auth, double)  # signed by kid-1
    double.published = {"kid-2": keys[1]}  # kid-1 withdrawn (for example after a compromise)
    double.clock += 3601  # the cache is stale, so the next use refreshes it
    rejected(auth, double)
    assert set(jwks.keys) == {"kid-2"}


def test_a_token_naming_an_unknown_key_cannot_drive_repeated_fetches(parts, double):
    auth, *_ = parts
    double.header = {"alg": "RS256", "kid": "unknown-kid"}
    for _ in range(6):
        rejected(auth, double)
    assert double.count(JWKS) == 1  # one fetch per throttle window, not one per bad token
    double.clock += 61
    rejected(auth, double)
    assert double.count(JWKS) == 2


def test_concurrent_lookups_share_one_fetch(parts, double):
    _, jwks, *_ = parts

    async def many():
        return await asyncio.gather(*(jwks.key_for("kid-1") for _ in range(8)))

    assert len(set(map(id, asyncio.run(many())))) == 1
    assert double.count(JWKS) == 1


def test_a_cached_key_survives_a_failed_refresh_but_only_for_a_day(parts, double):
    auth, jwks, *_ = parts
    login(auth, double)
    double.jwks_override = lambda: httpx.Response(503)
    double.clock += 3601
    login(auth, double)  # provider unreachable: the key fetched an hour ago is still honoured
    assert double.count(JWKS) == 2
    double.clock += 24 * 3600
    rejected(auth, double)


# -- tokens that must be refused ----------------------------------------------------------------------------


def test_a_known_kid_signed_by_another_key_is_refused(parts, double, keys):
    auth, *_ = parts
    double.signer = keys[3]  # an attacker's key under the published key id
    rejected(auth, double)


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none", "kid": "kid-1"},
        {"alg": "HS256", "kid": "kid-1"},
        {"alg": "RS512", "kid": "kid-1"},
        {"alg": "RS256", "kid": "kid-1", "jku": "https://evil.invalid/keys"},
        {"alg": "RS256", "kid": "kid-1", "jwk": {"kty": "RSA"}},
        {"alg": "RS256", "kid": "kid-1", "x5u": "https://evil.invalid/cert"},
        {"alg": "RS256"},
        {"alg": "RS256", "kid": 1},
        {"alg": "RS256", "kid": "../../etc"},
        {"alg": "RS256", "kid": "k" * 129},
    ],
    ids=["none", "hs256", "rs512", "jku", "jwk", "x5u", "no_kid", "int_kid", "path_kid", "long_kid"],
)
def test_bad_headers_are_refused_before_any_key_is_looked_up(parts, double, keys, header):
    auth, *_ = parts
    pem = keys[0].public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    head = b64(json.dumps(header).encode())
    body = b64(json.dumps({"iss": ISSUER, "sub": OWNER, "aud": CLIENT}).encode())
    # algorithm confusion: an HMAC keyed with the public key text
    mac = hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest()
    double.token = f"{head}.{body}.{b64(mac)}"
    rejected(auth, double)
    assert double.count(JWKS) == 0  # an unverified header never causes a network fetch


@pytest.mark.parametrize(
    "claims",
    [{"iss": "https://other.invalid/pool-1"}, {"aud": "other-client"}, {"sub": "someone-else"}, {"nonce": "x"}],
    ids=["issuer", "audience", "subject", "nonce"],
)
def test_claims_are_still_enforced_with_a_real_key_set(parts, double, claims):
    auth, *_ = parts
    double.claims = claims
    rejected(auth, double)


def test_an_expired_token_is_refused(parts, double):
    auth, *_ = parts
    double.claims = {"iat": double.now() - 700, "exp": double.now() - 100}
    rejected(auth, double)


def test_an_overlong_token_lifetime_is_refused(parts, double):
    auth, *_ = parts
    double.claims = {"exp": double.now() + 3600}  # a provider default of one hour exceeds the 600 s bound
    rejected(auth, double)


def test_a_replayed_callback_is_refused(parts, double):
    auth, *_ = parts
    browser, url = auth.begin(None)
    query = parse_qs(urlsplit(url).query)
    double.nonce = query["nonce"][0]
    asyncio.run(auth.complete(browser, query["state"][0], "invented-code"))
    with pytest.raises(RefreshRejected):
        asyncio.run(auth.complete(browser, query["state"][0], "invented-code"))


# -- key set documents ----------------------------------------------------------------------------------------


def good(keys, count=1):
    return {"keys": [jwk(f"kid-{index}", keys[index]) for index in range(count)]}


def test_a_well_formed_key_set_parses(keys):
    parsed = parse_jwks(good(keys, 3))
    assert set(parsed) == {"kid-0", "kid-1", "kid-2"}


def test_other_key_types_and_encryption_keys_are_ignored(keys):
    document = good(keys)
    document["keys"] += [
        {"kty": "EC", "kid": "ec-1", "crv": "P-256", "x": "AA", "y": "AA"},
        jwk("enc-1", keys[1], use="enc"),
    ]
    assert set(parse_jwks(document)) == {"kid-0"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d, k: d.update(keys=[]),
        lambda d, k: d.update(keys="none"),
        lambda d, k: d.update(keys=[jwk("a", k[0])] * 2),  # duplicate kid is ambiguous
        lambda d, k: d.update(keys=[jwk("a", k[0], e="Aw")]),  # e = 3
        lambda d, k: d.update(keys=[jwk("a", k[0], alg="RS512")]),
        lambda d, k: d.update(keys=[jwk("a", k[0], n="!!")]),
        lambda d, k: d.update(keys=[jwk("a", k[0], n="")]),
        lambda d, k: d.update(keys=[jwk("", k[0])]),
        lambda d, k: d.update(keys=[jwk("a b", k[0])]),
        lambda d, k: d.update(keys=["text"]),
        lambda d, k: d.update(keys=[jwk(f"k{i}", k[0]) for i in range(17)]),
        lambda d, k: d.update(keys=[{"kty": "EC", "kid": "only-ec"}]),  # nothing usable
        lambda d, k: d.update(keys=[jwk("small", rsa.generate_private_key(public_exponent=65537, key_size=1024))]),
    ],
    ids=[
        "empty",
        "not_a_list",
        "duplicate_kid",
        "small_exponent",
        "wrong_alg",
        "bad_modulus",
        "empty_modulus",
        "empty_kid",
        "space_kid",
        "non_object",
        "too_many",
        "nothing_usable",
        "small_key",
    ],
)
def test_malformed_or_weak_key_sets_are_refused(keys, mutate):
    document = good(keys)
    mutate(document, keys)
    with pytest.raises(ProviderError):
        parse_jwks(document)


@pytest.mark.parametrize(
    "response",
    [
        lambda: httpx.Response(500),
        lambda: httpx.Response(200, content=b"not json"),
        lambda: httpx.Response(200, json={"keys": []}),
        lambda: httpx.Response(200, content=b'{"keys": [], "keys": []}'),  # duplicate member
        lambda: httpx.Response(200, content=b"x" * (70 * 1024)),
    ],
    ids=["status", "not_json", "no_keys", "duplicate_member", "oversize"],
)
def test_a_bad_key_set_response_fails_closed_and_is_throttled(parts, double, response):
    auth, jwks, *_ = parts
    double.jwks_override = response
    rejected(auth, double)
    rejected(auth, double)
    assert double.count(JWKS) == 1 and jwks.keys == {}


# -- metadata and discovery ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "http://idp.invalid/pool-1"},
        {"issuer": ISSUER + "/"},
        {"issuer": "https://user:pw@idp.invalid/pool-1"},
        {"issuer": "https://idp.invalid/pool-1?x=1"},
        {"authorization_endpoint": "http://login.idp.invalid/oauth2/authorize"},
        {"authorization_endpoint": "https://login.idp.invalid/oauth2/authorize#frag"},
        {"token_endpoint": "https://login.idp.invalid/oauth2/token?x=1"},
        {"token_endpoint": "https://login.idp.invalid\\evil/token"},
        {"jwks_uri": "https://login.idp.invalid/a/../keys"},
        {"jwks_uri": "https://login.idp.invalid:99999/keys"},
        {"jwks_uri": "login.idp.invalid/keys"},
        {"jwks_uri": None},
        {"jwks_uri": "https://login.idp.invalid/" + "a" * 600},
    ],
    ids=[
        "http_issuer",
        "slash_issuer",
        "credentials",
        "query_issuer",
        "http_authorize",
        "fragment",
        "query_token",
        "backslash",
        "dot_segment",
        "bad_port",
        "no_scheme",
        "none",
        "overlong",
    ],
)
def test_unsafe_provider_metadata_is_refused(change):
    values = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZE,
        "token_endpoint": TOKEN,
        "jwks_uri": JWKS,
    } | change
    with pytest.raises(ProviderError):
        ProviderMetadata(**values)


def test_discovery_returns_metadata_equal_to_the_pin(double):
    found = asyncio.run(discover(double.transport(), ISSUER, pinned=METADATA))
    assert found == METADATA and double.urls == [DISCOVERY]


@pytest.mark.parametrize(
    "change,code",
    [
        ({"issuer": "https://other.invalid/pool-1"}, "issuer_mismatch"),
        ({"jwks_uri": "https://evil.invalid/keys"}, "metadata_changed"),
        ({"token_endpoint": "https://evil.invalid/oauth2/token"}, "metadata_changed"),
        ({"id_token_signing_alg_values_supported": ["HS256"]}, "provider_metadata"),
        ({"code_challenge_methods_supported": ["plain"]}, "provider_metadata"),
        ({"authorization_endpoint": "http://login.idp.invalid/oauth2/authorize"}, "provider_metadata"),
    ],
    ids=["issuer", "jwks_moved", "token_moved", "hs256_only", "no_s256", "http_endpoint"],
)
def test_discovery_that_differs_from_the_pin_or_the_rules_is_refused(double, change, code):
    double.discovery |= change
    with pytest.raises(ProviderError) as raised:
        asyncio.run(discover(double.transport(), ISSUER, pinned=METADATA))
    assert raised.value.code == code


def test_discovery_failure_is_refused(double):
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with pytest.raises(ProviderError) as raised:
        asyncio.run(discover(transport, ISSUER))
    assert raised.value.code == "discovery_failed"


def test_only_an_in_process_transport_can_be_used_so_nothing_here_can_reach_a_network(keys):
    real = httpx.AsyncHTTPTransport()
    with pytest.raises(TypeError):
        asyncio.run(discover(real, ISSUER))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        asyncio.run(JwksKeys(real, JWKS).key_for("kid-1"))  # type: ignore[arg-type]


# -- construction guards ------------------------------------------------------------------------------------


def test_owner_auth_construction_guards(tmp_path, double, keys):
    transport = double.transport()
    catalog = Catalog(tmp_path / "guards.sqlite", lambda: double.clock)
    try:
        jwks = JwksKeys(transport, JWKS)
        other = ProviderMetadata("https://other.invalid", AUTHORIZE, TOKEN, JWKS)
        elsewhere = JwksKeys(transport, "https://idp.invalid/other-keys")
        public = keys[0].public_key()
        for kwargs, key in (
            ({"provider": METADATA}, None),  # metadata without a key source
            ({"keys": jwks}, None),  # a key source without metadata
            ({"provider": METADATA, "keys": jwks}, public),  # a static key may not linger beside a provider
            ({"provider": other, "keys": jwks}, None),  # issuer must equal the owner identity's
            ({"provider": METADATA, "keys": elsewhere}, None),  # the key source must be the pinned jwks_uri
            ({}, None),  # no provider and no static key
        ):
            with pytest.raises(ValueError):
                OwnerAuth(catalog, key, transport, IDENTITY, **kwargs)
    finally:
        catalog.close()


# -- through the Beta app -----------------------------------------------------------------------------------


def test_the_beta_app_signs_the_owner_in_with_provider_metadata_and_refuses_a_forged_key(tmp_path, double, keys):
    transport = double.transport()
    app = create_mock_beta_app(
        tmp_path / "control.sqlite",
        oidc_key=None,
        epic_key=keys[1],
        oidc_transport=transport,
        epic_transport=httpx.MockTransport(lambda request: pytest.fail("no Epic request is possible here")),
        clock=lambda: double.clock,
        identity=IDENTITY,
        provider=METADATA,
        jwks=JwksKeys(transport, JWKS, lambda: double.clock),
    )

    def attempt(client):
        start = client.post("/beta/auth/login", headers={"Origin": ORIGIN})
        assert start.status_code == 303
        target = urlsplit(start.headers["location"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == AUTHORIZE
        query = parse_qs(target.query)
        double.nonce = query["nonce"][0]
        return client.get("/beta/auth/callback", params={"state": query["state"][0], "code": "invented-code"})

    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        double.signer = keys[3]  # not the published key
        assert attempt(client).status_code == 401
        assert client.get("/beta/epic/patients/mock-alpha/status").status_code == 401
        double.signer = keys[0]
        result = attempt(client)
        assert result.status_code == 200 and "__Host-beta-session=" in result.headers["set-cookie"]
        assert client.get("/beta/epic/patients/mock-alpha/status").status_code != 401
