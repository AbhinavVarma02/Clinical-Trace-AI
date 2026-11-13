"""Focused offline durability tests using only the existing ephemeral synthetic signer."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

from api import beta_webauthn_store as storage
from api.beta_webauthn import CEREMONY_S, LocalOwnerAuth, LocalOwnerPolicy
from src.interop.epic.beta_catalog import RefreshRejected
from tests.test_beta_webauthn import BROWSER, HANDLE, ORIGIN, Clock, SyntheticAuthenticator, enroll, login, rejected

PRINCIPAL = "synthetic-os-owner"


@pytest.fixture
def durable(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "current_principal", lambda: PRINCIPAL)
    policy = LocalOwnerPolicy(ORIGIN, HANDLE)
    store = storage.OwnerCredentialStore.provision(
        tmp_path / "owner", policy, PRINCIPAL, authorize=lambda: True, synthetic_only=True
    )
    owner = LocalOwnerAuth(policy, store=store, clock=Clock(), authorize_enrollment=lambda: True)
    yield owner, store, SyntheticAuthenticator()
    store.close()


def reopen(store):
    store.close()
    next_store = storage.OwnerCredentialStore(store.root, store.policy, PRINCIPAL)
    owner = LocalOwnerAuth(store.policy, store=next_store, clock=Clock(), authorize_enrollment=lambda: True)
    return next_store, owner


@pytest.mark.parametrize("case", ["default", "no_authorizer", "denied", "not_true", "wrong_principal"])
def test_provision_is_explicit_synthetic_and_os_owner_authorized(tmp_path, monkeypatch, case):
    monkeypatch.setattr(storage, "current_principal", lambda: PRINCIPAL)
    kwargs = {"synthetic_only": True, "authorize": lambda: True}
    if case == "default":
        kwargs.pop("synthetic_only")
    elif case == "no_authorizer":
        kwargs.pop("authorize")
    elif case == "denied":
        kwargs["authorize"] = lambda: False
    elif case == "not_true":
        kwargs["authorize"] = lambda: 1
    root = tmp_path / "owner"
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore.provision(
            root, LocalOwnerPolicy(ORIGIN, HANDLE), "another-user" if case == "wrong_principal" else PRINCIPAL, **kwargs
        )
    assert not root.exists()


def test_existing_or_missing_store_is_never_automatically_initialized(durable):
    _, store, _ = durable
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore.provision(
            store.root, store.policy, PRINCIPAL, authorize=lambda: True, synthetic_only=True
        )
    store.close()
    store.path.unlink()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(store.root, store.policy, PRINCIPAL)
    assert not store.path.exists()


def test_pinned_key_counter_and_revocation_survive_restart_but_sessions_and_proofs_do_not(durable):
    owner, store, key = durable
    enroll(owner, key)
    token, session = login(owner, key)
    step = owner.begin_step_up(token, session.csrf, BROWSER)
    owner.complete_step_up(step["flow_id"], BROWSER, key.response(step), token, session.csrf)
    unfinished = owner.begin_login(BROWSER)
    state = store.load()
    next_store, next_owner = reopen(store)
    try:
        assert next_store.load() == state and state.counter == 2
        assert next_owner.session(token) is None
        assert not next_owner.consume_elevation(token, session.csrf)
        rejected(lambda: next_owner.begin_registration("x" * 43, BROWSER), "owner_already_enrolled")
        rejected(
            lambda: next_owner.complete_login(unfinished["flow_id"], BROWSER, key.response(unfinished)),
            "ceremony_rejected",
        )
        current, current_session = login(next_owner, key)
        step = next_owner.begin_step_up(current, current_session.csrf, BROWSER)
        next_owner.complete_step_up(step["flow_id"], BROWSER, key.response(step), current, current_session.csrf)
        assert next_owner.consume_elevation(current, current_session.csrf)
        assert not next_owner.consume_elevation(current, current_session.csrf)
        next_owner.revoke_owner()
        assert next_owner.session(current) is None
        final_store, revoked = reopen(next_store)
        try:
            assert final_store.load().revoked
            rejected(revoked.issue_enrollment_grant, "owner_revoked")
            rejected(lambda: revoked.begin_login(BROWSER), "owner_revoked")
        finally:
            final_store.close()
    finally:
        next_store.close()


def test_durable_store_does_not_replace_the_required_out_of_band_enrollment_authorizer(durable):
    _, store, _ = durable
    denied = LocalOwnerAuth(store.policy, store=store)
    rejected(denied.issue_enrollment_grant, "enrollment_not_authorized")
    assert store.load().credential is None


@pytest.mark.parametrize("purpose", ["login", "step-up"])
def test_durable_write_cannot_extend_a_ceremony_deadline(durable, monkeypatch, purpose):
    owner, store, key = durable
    enroll(owner, key)
    token, session = login(owner, key) if purpose == "step-up" else (None, None)
    options = owner.begin_step_up(token, session.csrf, BROWSER) if session else owner.begin_login(BROWSER)
    advance = store.advance

    def slow_commit(state, counter):
        result = advance(state, counter)
        owner.clock.now += CEREMONY_S
        return result

    monkeypatch.setattr(store, "advance", slow_commit)
    payload = key.response(options)
    if purpose == "login":
        rejected(lambda: owner.complete_login(options["flow_id"], BROWSER, payload), "authentication_rejected")
        assert not owner.sessions
    else:
        rejected(
            lambda: owner.complete_step_up(options["flow_id"], BROWSER, payload, token, session.csrf),
            "authentication_rejected",
        )
        assert not owner.consume_elevation(token, session.csrf)


def test_counter_rollback_is_rejected_after_restart_and_zero_counters_remain_valid(durable):
    owner, store, key = durable
    enroll(owner, key)
    login(owner, key)
    next_store, next_owner = reopen(store)
    try:
        flow = next_owner.begin_login(BROWSER)
        rejected(
            lambda: next_owner.complete_login(flow["flow_id"], BROWSER, key.response(flow, attack="counter")),
            "authentication_rejected",
        )
        assert next_store.load().counter == 1 and not next_owner.sessions
    finally:
        next_store.close()


def test_zero_counter_updates_are_committed_and_replay_still_fails_across_restart(durable):
    owner, store, key = durable
    enroll(owner, key)
    options = owner.begin_login(BROWSER)
    payload = key.response(options, attack="counter")
    owner.complete_login(options["flow_id"], BROWSER, payload)
    next_store, next_owner = reopen(store)
    try:
        rejected(lambda: next_owner.complete_login(options["flow_id"], BROWSER, payload), "ceremony_rejected")
        options = next_owner.begin_login(BROWSER)
        token, _ = next_owner.complete_login(options["flow_id"], BROWSER, key.response(options, attack="counter"))
        assert next_owner.session(token) is not None
        assert next_store.load().counter == 0 and next_store.load().revision == 3
    finally:
        next_store.close()


@pytest.mark.parametrize("operation", ["enroll", "login", "step-up", "revoke"])
def test_failed_write_never_grants_access_and_interrupted_mutation_blocks_restart(durable, operation):
    owner, store, key = durable
    if operation != "enroll":
        enroll(owner, key)
    token, session = login(owner, key) if operation in {"step-up", "revoke"} else (None, None)
    if operation == "enroll":
        options = owner.begin_registration(owner.issue_enrollment_grant(), BROWSER)
        payload = key.response(options, register=True)

        def action():
            owner.complete_registration(options["flow_id"], BROWSER, payload)
    elif operation == "login":
        options = owner.begin_login(BROWSER)

        def action():
            owner.complete_login(options["flow_id"], BROWSER, key.response(options))
    elif operation == "step-up":
        options = owner.begin_step_up(token, session.csrf, BROWSER)

        def action():
            owner.complete_step_up(options["flow_id"], BROWSER, key.response(options), token, session.csrf)
    else:
        action = owner.revoke_owner
    # A real SQLite write denial, after the durable uncertainty marker and proof verification.
    store._connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE else 0)
    rejected(action, "owner_storage_unavailable")
    assert not owner.sessions and not owner._grants and not owner._pending
    assert (store.root / "mutation.pending").is_file()
    rejected(lambda: owner.begin_login(BROWSER), "owner_storage_unavailable")
    store.close()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(store.root, store.policy, PRINCIPAL)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "bytes",
        "origin",
        "handle",
        "principal",
        "purpose",
        "version",
        "counter",
        "revoked",
        "revision",
        "schema",
        "cleared_credential",
    ],
)
def test_corrupt_or_wrongly_bound_state_fails_closed_without_reinitialization(durable, corruption):
    owner, store, key = durable
    enroll(owner, key)
    store.close()
    if corruption == "missing":
        store.path.unlink()
    elif corruption == "bytes":
        store.path.write_bytes(b"invalid SQLite data")
    else:
        with sqlite3.connect(store.path) as connection:
            mutations = {
                "origin": ("origin", "https://other.invalid"),
                "handle": ("handle", b"another-owner-handle"),
                "principal": ("principal", "another-user"),
                "purpose": ("purpose", "genuine"),
                "version": ("version", 2),
                "counter": ("counter", -1),
                "revoked": ("revoked", 3),
                "revision": ("revision", -1),
                "cleared_credential": ("credential", None),
            }
            if corruption == "schema":
                connection.execute("CREATE TABLE unexpected(value TEXT)")
            else:
                column, value = mutations[corruption]
                connection.execute(f"UPDATE owner_state SET {column}=?", (value,))
        connection.close()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(store.root, store.policy, PRINCIPAL)


def test_malformed_persisted_webauthn_public_key_is_not_treated_as_an_empty_store(durable):
    _, store, _ = durable
    store.close()
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE owner_state SET credential=?, revision=1", (b"invalid-attested-data",))
    connection.close()
    next_store = storage.OwnerCredentialStore(store.root, store.policy, PRINCIPAL)
    try:
        with pytest.raises(RefreshRejected) as error:
            LocalOwnerAuth(store.policy, store=next_store)
        assert error.value.code == "owner_storage_unavailable"
    finally:
        next_store.close()


def test_second_connection_cannot_open_an_active_store(durable):
    _, store, _ = durable
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(store.root, store.policy, PRINCIPAL)
    assert store.load().revision == 0


def test_stale_state_cannot_overwrite_the_committed_counter(durable):
    owner, store, key = durable
    enroll(owner, key)
    stale = store.load()
    login(owner, key)
    with pytest.raises(storage.StoreUnavailable):
        store.advance(stale, 2)
    assert not owner.session(next(iter(owner.sessions)))


@pytest.mark.parametrize("failure", ["identity", "acl", "missing", "hardlink"])
def test_existing_sessions_and_elevations_fail_closed_when_custody_is_lost(durable, monkeypatch, failure):
    owner, store, key = durable
    enroll(owner, key)
    token, session = login(owner, key)
    step = owner.begin_step_up(token, session.csrf, BROWSER)
    owner.complete_step_up(step["flow_id"], BROWSER, key.response(step), token, session.csrf)
    if failure == "identity":
        monkeypatch.setattr(storage, "current_principal", lambda: "another-user")
    elif failure == "acl":
        monkeypatch.setattr(storage.private, "verify_private_path", lambda _: (_ for _ in ()).throw(PermissionError()))
    elif failure == "missing":
        # Simulate disappearance without weakening Windows' protection of the exclusively open database.
        monkeypatch.setattr(store.path.__class__, "lstat", lambda _: (_ for _ in ()).throw(FileNotFoundError()))
    else:
        try:
            os.link(store.path, store.root / "unexpected-hardlink")
        except PermissionError as error:
            # The new no-delete file pin prevents this injection before the old nlink guard.
            # Accept only a real Windows sharing violation, not a sandbox/access-policy failure.
            if sys.platform != "win32" or error.winerror != 32:
                raise
            assert store.path.stat().st_nlink == 1
            assert owner.session(token) is session
            assert owner.consume_elevation(token, session.csrf)
            assert not owner.consume_elevation(token, session.csrf)
            return
    assert owner.session(token) is None and not owner.consume_elevation(token, session.csrf)
    assert not owner.sessions and not owner._pending and not owner._grants


def test_store_contains_no_durable_session_csrf_grant_challenge_or_elevation(durable):
    owner, store, key = durable
    grant = owner.issue_enrollment_grant()
    options = owner.begin_registration(grant, BROWSER)
    owner.complete_registration(options["flow_id"], BROWSER, key.response(options, register=True))
    token, session = login(owner, key)
    step = owner.begin_step_up(token, session.csrf, BROWSER)
    owner.complete_step_up(step["flow_id"], BROWSER, key.response(step), token, session.csrf)
    raw = store.path.read_bytes()
    for secret in (
        grant,
        options["flow_id"],
        options["publicKey"]["challenge"],
        token,
        session.csrf,
        BROWSER,
        step["flow_id"],
    ):
        assert secret.encode() not in raw


@pytest.mark.parametrize("interrupted", [False, True])
def test_process_exit_preserves_committed_revocation_or_refuses_uncertain_state(durable, interrupted):
    owner, store, key = durable
    enroll(owner, key)
    store.close()
    script = """
import os, sys
import netguard
netguard.install()
import sqlite3
from api import beta_webauthn_store as storage
from api.beta_webauthn import LocalOwnerPolicy
from pathlib import Path
storage.current_principal = lambda: 'synthetic-os-owner'
policy = LocalOwnerPolicy('https://localhost:8443', b'synthetic-approved-owner-handle-0')
store = storage.OwnerCredentialStore(Path(sys.argv[1]), policy, 'synthetic-os-owner')
if sys.argv[2] == 'True':
    store._connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE else 0)
try:
    store.revoke(store.load())
except storage.StoreUnavailable:
    os._exit(19 if not netguard.ATTEMPTS else 99)
os._exit(0 if not netguard.ATTEMPTS else 99)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.root), str(interrupted)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == (19 if interrupted else 0), result.stderr.decode(errors="replace")
    if interrupted:
        with pytest.raises(storage.StoreUnavailable):
            storage.OwnerCredentialStore(store.root, store.policy, PRINCIPAL)
    else:
        next_store = storage.OwnerCredentialStore(store.root, store.policy, PRINCIPAL)
        try:
            assert next_store.load().revoked
        finally:
            next_store.close()
