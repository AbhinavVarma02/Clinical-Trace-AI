"""AUTH-1 (offline): owner-identity configuration, step-up reauthentication and the one-shot elevation LIVE approve needs.

Everything here uses the invented in-process identity provider and the offline socket guard. It proves the relying
party's claim handling, not a real identity provider (AUTH-2) and not production readiness.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from api.beta_oidc import AUDIENCE, ISSUER, SUBJECT, INVENTED_IDENTITY, AuthClaims, OwnerAuth
from tests.test_beta_live_generations import FixtureLive, routed as routed_fixture
from tests.test_beta_live_route import (
    LIVE,
    begin_step_up,
    build,
    fake as fake_fixture,
    refresh_and_wait,
    step_up,
)
from tests.test_beta_owner_refresh import harness as owner_fixture, keys as keys_fixture

harness = owner_fixture
keys = keys_fixture
fake = fake_fixture
routed = routed_fixture
assert FixtureLive  # imported so the `routed` fixture's monkeypatch target exists


def staged_candidate(harness, client):
    candidate = refresh_and_wait(harness, client)["candidate"]
    return (
        f"{LIVE}/candidate/{candidate['attempt_id']}/approve",
        {"X-Beta-Candidate-Hash": candidate["manifest_sha256"]},
    )


def code(response):
    return response.json()["error"]["code"]


# -- owner identity configuration ----------------------------------------------------------------------------


def test_the_default_identity_is_the_invented_offline_one_and_is_immutable():
    assert (INVENTED_IDENTITY.issuer, INVENTED_IDENTITY.client_id, INVENTED_IDENTITY.owner_subject) == (
        ISSUER,
        AUDIENCE,
        SUBJECT,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        INVENTED_IDENTITY.owner_subject = "someone-else"  # type: ignore[misc]


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "http://idp.example"},
        {"issuer": "https://idp.example/"},
        {"issuer": "https://idp.example?x=1"},
        {"issuer": "idp.example"},
        {"owner_subject": ""},
        {"client_id": ""},
        {"owner_subject": ["a", "b"]},  # exactly one subject, never a list
        {"required_amr": frozenset(), "required_acr": None},
        {"step_up_ttl_s": 0},
        {"step_up_ttl_s": 301},
        {"max_auth_age_s": 0},
    ],
    ids=lambda change: next(iter(change)) + "=" + str(next(iter(change.values())))[:18],
)
def test_an_unsafe_identity_configuration_is_refused(change):
    with pytest.raises(ValueError):
        dataclasses.replace(INVENTED_IDENTITY, **change)


# -- step-up: who can ask, what the provider is told ---------------------------------------------------------


def test_step_up_needs_a_signed_in_owner_and_exists_only_with_live(harness, keys, fake):
    with build(harness, keys, fake) as client:
        assert harness.post(client, "/beta/auth/step-up").status_code == 401  # not signed in
        harness.login(client)
        assert harness.post(client, "/beta/auth/step-up").status_code == 303
        assert client.post("/beta/auth/step-up", headers={"Origin": "https://beta.invalid"}).status_code == 403
    with build(harness, keys, fake, live=False) as client:
        harness.login(client)
        assert harness.post(client, "/beta/auth/step-up").status_code == 404  # MOCK-only setups have no such route


def test_step_up_tells_the_provider_to_authenticate_again(harness, keys, fake):
    with build(harness, keys, fake) as client:
        harness.login(client)
        begin_step_up(harness, client)
        request = harness.step_up_request
        assert request["prompt"] == ["login"] and request["max_age"] == ["0"]
        assert request["code_challenge_method"] == ["S256"] and request["scope"] == ["openid"]
        harness.begin(client)  # an ordinary login asks for neither
        assert "prompt" not in harness.auth_request and "max_age" not in harness.auth_request


# -- the claim matrix ----------------------------------------------------------------------------------------

GOOD = {"auth_time": 0, "acr": "mfa", "amr": ["pwd", "mfa"]}  # auth_time is an offset from "now"

REJECTED = {
    "missing_auth_time": {"auth_time": None},
    "stale_auth_time": {"auth_time": -60},
    "future_auth_time": {"auth_time": 60},
    "boolean_auth_time": {"auth_time": True},
    "string_auth_time": {"auth_time": "now"},
    "no_amr": {"amr": None},
    "weak_amr": {"amr": ["pwd"]},
    "amr_not_a_list": {"amr": "mfa"},
    "amr_with_non_string": {"amr": ["mfa", 1]},
    "wrong_subject": {"sub": "someone-else"},
    "wrong_issuer": {"iss": "https://other-idp.invalid"},
    "wrong_audience": {"aud": "other-client"},
    "wrong_authorized_party": {"azp": "other-client"},
    "wrong_nonce": {"nonce": "not-the-flow-nonce"},
    "not_yet_valid": {"nbf": 10**10},
    "overlong_lifetime": {"exp": 10**10},
}
ACCEPTED = {
    "baseline": {},
    "mfa_alone": {"amr": ["mfa"]},
    "inside_skew": {"auth_time": -3},
}


def attempt(harness, client, overrides):
    values = dict(GOOD) | overrides
    if isinstance(values["auth_time"], int) and not isinstance(values["auth_time"], bool):
        values["auth_time"] += int(harness.clock)
    state = begin_step_up(harness, client)
    harness.claims = values
    try:
        return harness.finish(client, state)
    finally:
        harness.claims = {}


@pytest.mark.parametrize("name", list(REJECTED))
def test_a_weak_or_wrong_step_up_grants_nothing(harness, keys, fake, routed, name):
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve, headers = staged_candidate(harness, client)
        response = attempt(harness, client, REJECTED[name])
        assert (response.status_code, code(response)) == (401, "step_up_rejected"), name
        assert client.get(f"{LIVE}/status").status_code == 200  # a failed step-up does not end the session
        denied = harness.post(client, approve, **headers)
        assert (denied.status_code, code(denied)) == (401, "step_up_required")


@pytest.mark.parametrize("name", list(ACCEPTED))
def test_an_acceptable_step_up_grants_exactly_one_approval(harness, keys, fake, routed, name):
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve, headers = staged_candidate(harness, client)
        granted = attempt(harness, client, ACCEPTED[name])
        assert granted.status_code == 200 and granted.json() == {"step_up": "granted", "expires_in": 120}
        assert harness.post(client, approve, **headers).status_code == 201


def test_a_required_authentication_class_is_enforced(harness, keys, fake, routed):
    identity = dataclasses.replace(INVENTED_IDENTITY, required_amr=frozenset(), required_acr="urn:invented:mfa")
    with build(harness, keys, fake, identity=identity) as client:
        harness.login(client)
        assert attempt(harness, client, {"acr": "pwd-only"}).status_code == 401
        assert attempt(harness, client, {"acr": None}).status_code == 401
        assert attempt(harness, client, {"acr": "urn:invented:mfa", "amr": None}).status_code == 200


def test_the_configured_owner_subject_is_the_only_one_accepted(harness, keys, fake, routed):
    identity = dataclasses.replace(INVENTED_IDENTITY, owner_subject="a-different-owner")
    with build(harness, keys, fake, identity=identity) as client:
        assert (
            harness.finish(client, harness.begin(client)).status_code == 401
        )  # the invented subject is not this owner
        harness.claims = {"sub": "a-different-owner"}
        try:
            harness.login(client)
        finally:
            harness.claims = {}


# -- one-shot, short-lived, bound to this session ------------------------------------------------------------


def test_a_replayed_step_up_callback_is_refused(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        state = begin_step_up(harness, client)
        harness.claims = dict(GOOD) | {"auth_time": int(harness.clock)}
        assert harness.finish(client, state).status_code == 200
        approve, headers = staged_candidate(harness, client)
        harness.claims = dict(GOOD) | {"auth_time": int(harness.clock)}
        assert harness.finish(client, state).status_code == 401  # the flow was single-use
        harness.claims = {}
        assert harness.post(client, approve, **headers).status_code == 201  # the first grant still stands


@pytest.mark.parametrize(("elapsed", "allowed"), [(119, True), (121, False)])
def test_an_elevation_lasts_two_minutes(harness, keys, fake, routed, elapsed, allowed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve, headers = staged_candidate(harness, client)
        assert step_up(harness, client).status_code == 200
        harness.clock += elapsed
        response = harness.post(client, approve, **headers)
        assert (response.status_code == 201) is allowed
        if not allowed:
            assert code(response) == "step_up_required"


def test_an_elevation_is_spent_by_the_first_approval_attempt_whatever_its_outcome(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve, headers = staged_candidate(harness, client)
        assert step_up(harness, client).status_code == 200
        wrong = harness.post(client, approve, **{"X-Beta-Candidate-Hash": "f" * 64})
        assert code(wrong) == "candidate_hash_mismatch"
        assert code(harness.post(client, approve, **headers)) == "step_up_required"


def test_requests_refused_before_approval_do_not_spend_the_elevation(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve, headers = staged_candidate(harness, client)
        assert step_up(harness, client).status_code == 200
        assert harness.post(client, approve, **{"X-CSRF-Token": "forged"}, **headers).status_code == 403
        assert harness.post(client, approve, Origin="https://evil.invalid", **headers).status_code == 403
        assert harness.post(client, approve).status_code == 400  # no candidate hash
        assert harness.post(client, approve, **headers).status_code == 201


def test_an_elevation_does_not_survive_a_new_session(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve, headers = staged_candidate(harness, client)
        assert step_up(harness, client).status_code == 200
        harness.login(client)  # a new session replaces the elevated one
        assert code(harness.post(client, approve, **headers)) == "step_up_required"


def test_logout_discards_the_elevation(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve, headers = staged_candidate(harness, client)
        assert step_up(harness, client).status_code == 200
        assert harness.post(client, "/beta/auth/logout").status_code == 204
        harness.login(client)
        assert code(harness.post(client, approve, **headers)) == "step_up_required"


def test_a_step_up_flow_cannot_elevate_a_different_session(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve, headers = staged_candidate(harness, client)
        state = begin_step_up(harness, client)
        asked = client.cookies.get("__Host-beta-flow")
        for cookie in list(client.cookies.jar):
            if cookie.name == "__Host-beta-flow":
                client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)  # keep the flow alive server-side
        harness.login(client)  # a different session
        client.cookies.set("__Host-beta-flow", asked, domain="beta.invalid", path="/")
        harness.claims = dict(GOOD) | {"auth_time": int(harness.clock)}
        try:
            response = harness.finish(client, state)
        finally:
            harness.claims = {}
        assert (response.status_code, code(response)) == (401, "step_up_rejected")
        assert code(harness.post(client, approve, **headers)) == "step_up_required"


def test_a_step_up_without_its_session_grants_nothing(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        state = begin_step_up(harness, client)
        assert harness.post(client, "/beta/auth/logout").status_code == 204
        harness.claims = dict(GOOD) | {"auth_time": int(harness.clock)}
        try:
            assert harness.finish(client, state).status_code == 401
        finally:
            harness.claims = {}


def test_logging_in_does_not_elevate(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.claims = {"auth_time": int(harness.clock), "amr": ["pwd", "mfa"]}  # even a login with MFA claims
        harness.login(client)
        harness.claims = {}
        approve, headers = staged_candidate(harness, client)
        assert code(harness.post(client, approve, **headers)) == "step_up_required"  # logging in is not stepping up


# -- ordinary login: authentication claims are validated and stored ------------------------------------------


def capture_owner(monkeypatch):
    owners = []
    original = OwnerAuth.create_session

    def remember(owner, previous):
        owners.append(owner)
        return original(owner, previous)

    monkeypatch.setattr(OwnerAuth, "create_session", remember)
    return owners


def test_login_stores_the_authentication_claims_and_rejects_malformed_ones(harness, keys, fake, monkeypatch):
    owners = capture_owner(monkeypatch)
    with build(harness, keys, fake) as client:
        harness.login(client)
        token = client.cookies.get("__Host-beta-session")
        assert owners[0].sessions[token].auth == AuthClaims()  # the invented IdP sends none; none is invented here
        now = int(harness.clock)
        harness.claims = {"auth_time": now - 10, "acr": "mfa", "amr": ["pwd", "mfa"]}
        harness.login(client)
        token = client.cookies.get("__Host-beta-session")
        assert owners[0].sessions[token].auth == AuthClaims(now - 10, "mfa", ("pwd", "mfa"))
        for bad in ({"auth_time": now - 4000}, {"auth_time": now + 600}, {"auth_time": -1}, {"amr": "mfa"}, {"acr": 5}):
            harness.claims = bad
            assert harness.finish(client, harness.begin(client)).status_code == 401, bad
        harness.claims = {}


def test_mfa_can_be_demanded_at_login(harness, keys, fake):
    identity = dataclasses.replace(INVENTED_IDENTITY, mfa_at_login=True)
    with build(harness, keys, fake, identity=identity) as client:
        assert harness.finish(client, harness.begin(client)).status_code == 401  # no auth_time, no MFA
        harness.claims = {"auth_time": int(harness.clock), "amr": ["pwd"]}
        assert harness.finish(client, harness.begin(client)).status_code == 401
        harness.claims = {"auth_time": int(harness.clock), "amr": ["pwd", "mfa"]}
        assert harness.finish(client, harness.begin(client)).status_code == 200
        harness.claims = {}


# -- nothing here enables real promotion ---------------------------------------------------------------------


def test_no_code_path_sets_real_promotion():
    import inspect

    from src.interop.epic.beta_live_generations import LiveGenerations

    assert inspect.signature(LiveGenerations).parameters["real_promotion"].default is False
    root = Path(__file__).resolve().parent.parent
    offenders = [
        str(path.relative_to(root))
        for folder in ("api", "src")
        for path in (root / folder).rglob("*.py")
        if re.search(r"real_promotion\s*(=|:\s*bool\s*=)\s*True", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
