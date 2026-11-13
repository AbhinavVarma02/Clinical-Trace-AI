"""Invented SID, software signer and fake console/custody only. No genuine identity or Epic evidence.

The positive read-delivery probe uses a labelled CONTRACT DOUBLE, not relabeled provider evidence.
The actual existing synthetic generation is tested only for mandatory genuine-read rejection.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from api import beta_owner_console as console
from api import beta_owner_genuine as genuine
from api import beta_webauthn_store as storage
from api.beta_live_record import live_record_view
from api.beta_webauthn import OWNER_ORIGIN, LocalOwnerAuth, LocalOwnerPolicy
from api.beta_webauthn_http import create_local_owner_auth_app
from src.interop.epic.beta_catalog import RefreshRejected
from tests.test_beta_owner_origin import DedicatedSigner, INVENTED_SID, invented_sid_store
from tests.test_beta_synthetic_ui import _MemoryLoop
from tests.test_beta_webauthn import BROWSER, HANDLE, Clock


@pytest.fixture
def owned(tmp_path, monkeypatch):
    invented_sid_store.__wrapped__(monkeypatch)
    policy = LocalOwnerPolicy(OWNER_ORIGIN, HANDLE, storage.GENUINE_PURPOSE)
    store = storage.OwnerCredentialStore.provision_genuine(
        tmp_path / "invented-genuine-schema-only", policy, INVENTED_SID, authorize=lambda: True
    )
    app = genuine.create_genuine_owner_app(store, enabled=True, policy_accepted=True)
    app._auth.clock = Clock()
    yield app, store, DedicatedSigner()
    store.close()


def rejected(call, code):
    with pytest.raises(RefreshRejected) as error:
        call()
    assert error.value.code == code


def enrollment(app, monkeypatch):
    pending = app._action("enroll/pending", {"binding": BROWSER})
    monkeypatch.setattr(genuine, "confirm_owner_action", lambda *args, **kwargs: True)
    app.approve_enrollment(pending["pending_id"], pending["fingerprint"], enabled=True)
    context = {"pending_id": pending["pending_id"], "binding": BROWSER}
    return context, app._action("enroll/options", context)


def test_enroll_options_carry_every_required_webauthn_json_member(owned, monkeypatch):
    # Run 6c (2026-11-13): Chrome's parseCreationOptionsFromJSON threw before Windows Hello because
    # user.displayName (a required PublicKeyCredentialUserEntityJSON member) was absent.
    app, _, _ = owned
    _, options = enrollment(app, monkeypatch)
    public_key = json.loads(json.dumps(options, separators=(",", ":"), allow_nan=False))["publicKey"]
    assert all(
        isinstance(public_key["user"].get(key), str) and public_key["user"][key]
        for key in ("id", "name", "displayName")
    )
    assert all(isinstance(public_key["rp"].get(key), str) and public_key["rp"][key] for key in ("id", "name"))
    assert public_key["challenge"] and public_key["pubKeyCredParams"]
    selection = public_key["authenticatorSelection"]
    assert (selection["userVerification"], selection["authenticatorAttachment"]) == ("required", "platform")


def enroll(app, signer, monkeypatch):
    context, options = enrollment(app, monkeypatch)
    result = app._action(
        "enroll/finish", context | {"flow_id": options["flow_id"], "credential": signer.proof(options, register=True)}
    )
    assert result == {"enrolled": True} and not app._auth.sessions


def proof(app, signer, operation="verify"):
    options = app._action(operation + "/options", {})
    return {"flow_id": options["flow_id"], "credential": signer.proof(options)}


def http(app, action="verify/options", body=None, *, headers=None, scope_change=None, on_start=None):
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "client": ("127.0.0.1", 12345),
        "path": genuine.BASE + "/" + action,
        "raw_path": (genuine.BASE + "/" + action).encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"owner.clinical-trace.localhost:8443"),
            (b"origin", OWNER_ORIGIN.encode()),
            (b"content-type", b"application/json"),
        ]
        + [(key.encode(), value.encode()) for key, value in genuine.PROFILE.items()],
    }
    if headers:
        scope["headers"].extend(headers)
    if scope_change:
        scope.update(scope_change)
    result = []

    async def receive():
        return {"type": "http.request", "body": json.dumps({} if body is None else body).encode()}

    async def send(message):
        result.append(message)
        if message["type"] == "http.response.start" and on_start:
            on_start()

    asyncio.run(app(scope, receive, send), loop_factory=_MemoryLoop)
    return result[0], result[1]["body"]


def test_defaults_deny_before_os_or_paths(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("OS or path boundary reached")

    monkeypatch.setattr(storage, "current_principal", forbidden)
    monkeypatch.setattr(console, "current_principal", forbidden)
    monkeypatch.setattr(console, "_local_console", forbidden)
    policy = LocalOwnerPolicy(OWNER_ORIGIN, HANDLE, storage.GENUINE_PURPOSE)
    assert console.confirm_owner_action(INVENTED_SID, "a" * 64) is False
    with pytest.raises(ValueError):
        genuine.provision_owner_store(tmp_path / "never-created", INVENTED_SID)
    with pytest.raises(ValueError):
        genuine.create_genuine_owner_app()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore.provision_genuine(tmp_path / "never-created", policy, INVENTED_SID)
    assert not (tmp_path / "never-created").exists()


def test_factory_refuses_memory_substitutes_and_cookie_adapter(owned):
    app, store, _ = owned
    with pytest.raises(ValueError):
        LocalOwnerAuth(app.policy)
    with pytest.raises(ValueError):
        genuine.create_genuine_owner_app(store, enabled=True)
    with pytest.raises(ValueError):
        genuine.create_genuine_owner_app(object(), enabled=True, policy_accepted=True)
    with pytest.raises(ValueError):
        create_local_owner_auth_app(app._auth, enabled=True)
    assert app._auth.session("x" * 43) is None
    rejected(lambda: app._auth.begin_login(BROWSER), "bearer_sessions_disabled")
    rejected(lambda: app._auth.complete_login("x" * 43, BROWSER, {}), "bearer_sessions_disabled")
    rejected(lambda: app._action("read/options", {}), "live_reads_disabled")


def test_genuine_store_never_opens_or_converts_synthetic_state(tmp_path, monkeypatch):
    invented_sid_store.__wrapped__(monkeypatch)
    policy = LocalOwnerPolicy(OWNER_ORIGIN, HANDLE)
    store = storage.OwnerCredentialStore.provision(
        tmp_path / "synthetic", policy, INVENTED_SID, authorize=lambda: True, synthetic_only=True
    )
    with pytest.raises(ValueError):
        genuine.create_genuine_owner_app(store, enabled=True, policy_accepted=True)
    store.close()
    before = store.path.read_bytes()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(store.root, replace(policy, purpose=storage.GENUINE_PURPOSE), INVENTED_SID)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("case", ["unapproved", "wrong_fingerprint", "console_denied", "expired", "wrong_browser"])
def test_enrollment_authorization_is_internal_bound_expiring_default_deny(owned, monkeypatch, case):
    app, store, signer = owned
    pending = app._action("enroll/pending", {"binding": BROWSER})
    context = {"pending_id": pending["pending_id"], "binding": BROWSER}
    monkeypatch.setattr(genuine, "confirm_owner_action", lambda *args, **kwargs: case != "console_denied")
    if case == "expired":
        app._auth.clock.now += 61
    if case == "wrong_browser":
        app.approve_enrollment(pending["pending_id"], pending["fingerprint"], enabled=True)
        context["binding"] = "w" * 43
        rejected(lambda: app._action("enroll/options", context), "enrollment_rejected")
    elif case == "unapproved":
        rejected(
            lambda: app.approve_enrollment(pending["pending_id"], pending["fingerprint"]), "enrollment_not_authorized"
        )
        rejected(lambda: app._action("enroll/options", context), "enrollment_not_authorized")
    else:
        rejected(
            lambda: app.approve_enrollment(
                pending["pending_id"], "f" * 64 if case == "wrong_fingerprint" else pending["fingerprint"], enabled=True
            ),
            "enrollment_not_authorized" if case == "console_denied" else "enrollment_rejected",
        )
    assert store.load().credential is None and not app._auth.sessions


def test_genuine_enrollment_deadline_includes_durable_completion(owned, monkeypatch):
    app, store, signer = owned
    context, options = enrollment(app, monkeypatch)
    assert options["publicKey"]["authenticatorSelection"] == {
        "authenticatorAttachment": "platform",
        "residentKey": "discouraged",
        "requireResidentKey": False,
        "userVerification": "required",
    }
    original = store.enroll

    def slow(*args, valid):
        def after_commit():
            app._auth.clock.now += 61
            return valid()

        return original(*args, valid=after_commit)

    monkeypatch.setattr(store, "enroll", slow)
    rejected(
        lambda: app._action(
            "enroll/finish",
            context | {"flow_id": options["flow_id"], "credential": signer.proof(options, register=True)},
        ),
        "owner_storage_unavailable",
    )
    assert (store.root / "mutation.pending").is_file()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(store.root, store.policy, INVENTED_SID)
    rejected(lambda: app._action("verify/options", {}), "owner_storage_unavailable")
    assert not app._auth.sessions


def test_real_signatures_counter_replay_restart_and_durable_revocation(owned, monkeypatch):
    app, store, signer = owned
    enroll(app, signer, monkeypatch)
    signed = proof(app, signer)
    start, body = http(app, "verify/finish", signed)
    assert start["status"] == 200 and json.loads(body)["result"] == {"authenticated": True}
    assert b"set-cookie" not in dict(start["headers"]) and not app._auth.sessions
    rejected(lambda: app._action("verify/finish", signed), "ceremony_rejected")
    unfinished = proof(app, signer)
    store.close()
    reopened = storage.OwnerCredentialStore(store.root, store.policy, INVENTED_SID)
    try:
        next_app = genuine.create_genuine_owner_app(reopened, enabled=True, policy_accepted=True)
        next_app._auth.clock = app._auth.clock
        rejected(lambda: next_app._action("verify/finish", unfinished), "ceremony_rejected")
        next_app._action("verify/finish", proof(next_app, signer))
        next_app.revoke_owner(enabled=True)
        assert reopened.load().revoked
        rejected(lambda: next_app._action("verify/options", {}), "owner_revoked")
    finally:
        reopened.close()


@pytest.mark.parametrize("attack", ["origin", "rp", "no_uv", "signature", "expired", "operation", "store_failure"])
def test_proof_binding_and_fences(owned, monkeypatch, attack):
    app, store, signer = owned
    enroll(app, signer, monkeypatch)
    options = app._action("verify/options", {})
    credential = signer.proof(
        options,
        origin=OWNER_ORIGIN.replace(":8443", ":9443") if attack == "origin" else OWNER_ORIGIN,
        rp="localhost" if attack == "rp" else app.policy.rp_id,
        uv=attack != "no_uv",
    )
    if attack == "signature":
        credential["response"]["signature"] = "AAAA"
    if attack == "expired":
        app._auth.clock.now += 121
    if attack == "store_failure":
        monkeypatch.setattr(store, "load", lambda: (_ for _ in ()).throw(storage.StoreUnavailable()))
    if attack == "operation":
        rejected(lambda: app._auth.complete_request(options["flow_id"], b"x" * 32, credential), "ceremony_rejected")
    else:
        rejected(
            lambda: app._action("verify/finish", {"flow_id": options["flow_id"], "credential": credential}),
            "owner_storage_unavailable"
            if attack == "store_failure"
            else "ceremony_rejected"
            if attack == "expired"
            else "authentication_rejected",
        )
    assert not app._auth.sessions


@pytest.mark.parametrize(
    "attack",
    [
        "cookie",
        "bearer",
        "duplicate_origin",
        "duplicate_host",
        "forwarded",
        "port",
        "http",
        "query",
        "encoded",
        "foreign_path",
        "body",
    ],
)
def test_http_never_accepts_bearers_alternate_paths_or_cross_port_profile(owned, monkeypatch, attack):
    app, _, signer = owned
    enroll(app, signer, monkeypatch)
    changes, headers, body = {}, [], {}
    if attack == "cookie":
        headers = [(b"cookie", b"__Host-ct-owner-session=" + b"a" * 43)]
    if attack == "bearer":
        headers = [(b"authorization", b"Bearer " + b"a" * 43)]
    if attack == "duplicate_origin":
        headers = [(b"origin", OWNER_ORIGIN.encode())]
    if attack == "duplicate_host":
        headers = [(b"host", b"owner.clinical-trace.localhost:8443")]
    if attack == "forwarded":
        headers = [(b"x-forwarded-host", b"owner.clinical-trace.localhost:8443")]
    if attack == "port":
        headers = [(b"origin", b"https://owner.clinical-trace.localhost:9443")]
    if attack == "http":
        changes = {"scheme": "http"}
    if attack == "query":
        changes = {"query_string": b"token=guess"}
    if attack == "encoded":
        changes = {"raw_path": b"/owner/genuine/%76erify/options"}
    if attack == "foreign_path":
        changes = {"path": "/beta/auth/session", "raw_path": b"/beta/auth/session"}
    if attack == "body":
        body = {"alias": "unapproved"}
    start, raw = http(app, headers=headers, scope_change=changes, body=body)
    assert start["status"] in {400, 403, 404} and b"publicKey" not in raw
    assert not app._auth._pending and b"set-cookie" not in dict(start["headers"])


def test_revocation_between_headers_and_body_withholds_protected_result(owned, monkeypatch):
    app, _, signer = owned
    enroll(app, signer, monkeypatch)
    start, body = http(app, "verify/finish", proof(app, signer), on_start=app._auth.revoke_owner)
    assert start["status"] == 200 and body == b""


@pytest.mark.parametrize("case", ["redirected", "wrong_sid", "denied", "timeout", "lost_console"])
def test_console_fails_closed_using_only_invented_os_boundaries(monkeypatch, case):
    calls = []
    monkeypatch.setattr(console, "current_principal", lambda: "other-invented" if case == "wrong_sid" else INVENTED_SID)

    def local():
        calls.append(1)
        return case != "redirected" and not (case == "lost_console" and len(calls) > 1)

    monkeypatch.setattr(console, "_local_console", local)
    monkeypatch.setattr("builtins.input", lambda prompt: "deny" if case == "denied" else "approve " + "a" * 64)
    moments = iter([0, 61 if case == "timeout" else 1])
    monkeypatch.setattr(console.time, "monotonic", lambda: next(moments))
    assert not console.confirm_owner_action(INVENTED_SID, "a" * 64, enabled=True)


def test_genuine_projection_refuses_existing_synthetic_marker(monkeypatch):
    class SyntheticGenerationContract:
        def project_active(self, alias, authorized, project):
            return project({"synthetic_transport": True}, b"", {})

    with pytest.raises(RefreshRejected) as error:
        live_record_view(SyntheticGenerationContract(), "invented", authorized=lambda: True, genuine_only=True)
    assert error.value.code == "genuine_evidence_required"


def test_https_preparation_reuses_bootstrap_without_running_listener(owned, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from api import beta_owner_https as https

    app, _, _ = owned
    calls = []
    config = SimpleNamespace(ssl=object(), host="127.0.0.1", port=8443)
    monkeypatch.setattr(https, "prepare_bootstrap", lambda *args, **kwargs: calls.append((args, kwargs)) or config)
    with pytest.raises(ValueError):
        genuine.prepare_genuine_owner_https(app, tmp_path / "not-read.pem", tmp_path / "not-read.key")
    assert not calls
    assert (
        genuine.prepare_genuine_owner_https(app, tmp_path / "not-read.pem", tmp_path / "not-read.key", enabled=True)
        is config
    )
    assert config.app is app and config.loaded_app is app and len(calls) == 1


@pytest.fixture
def contract_read(owned, monkeypatch):
    """Only a result-path double, deliberately NOT a provider record or an invented-LIVE fixture promotion."""
    from src.interop.epic.beta_live_staging import LiveAllowlist

    app, store, signer = owned
    enroll(app, signer, monkeypatch)

    class ReadContract:
        allowlist = LiveAllowlist({"approved-alpha": "invented-live-alpha"})
        generation = "a" * 32
        attempt = "x" * 32
        expired = False

        def project_active(self, alias, authorized, project):
            if self.expired or self.allowlist.get(alias) is None or not authorized():
                raise RefreshRejected(401, "contract_refused")
            return project(
                {"generation_id": self.generation, "bundle_sha256": "b" * 64, "synthetic_transport": False},
                b"",
                {"manifest_sha256": "c" * 64, "attempt_id": self.attempt},
            )

    reads = ReadContract()
    app._reads, app._live_reads_approved = reads, True  # TEST ONLY; the factory rejects this substitute
    app._approved_evidence = genuine.LiveEvidenceBinding(reads.attempt, reads.generation, "c" * 64, "b" * 64)

    def projection(source, alias, *, authorized, genuine_only, approved):
        assert source is reads and genuine_only is True and authorized()
        return {
            "mode": "LIVE",
            "data_origin": "TEST_CONTRACT_DOUBLE",
            "alias": alias,
            "freshness": "fresh",
            "record": {"contract_sentinel": "INVENTED RESULT DELIVERY ONLY"},
            "warnings": [{"code": "live_access_disabled"}],
            "provenance": reads.project_active(
                alias, authorized, lambda manifest, bundle, row: approved.require(manifest, row)
            ),
        }

    monkeypatch.setattr(genuine, "live_record_view", projection)
    return app, store, signer, reads


def test_read_proof_returns_direct_result_once_without_result_handle_or_session(contract_read):
    app, store, signer, _ = contract_read
    signed = proof(app, signer, "read")
    start, raw = http(app, "read/finish", signed)
    payload = json.loads(raw)
    assert (
        start["status"] == 200 and payload["result"]["record"]["contract_sentinel"] == "INVENTED RESULT DELIVERY ONLY"
    )
    assert (
        set(payload) == {"result", "server_time", "display_until"}
        and payload["display_until"] - payload["server_time"] <= 30
    )
    assert not app._auth.sessions and store.load().counter == 1 and b"set-cookie" not in dict(start["headers"])
    start, raw = http(app, "read/finish", signed)
    assert start["status"] == 401 and b"contract_sentinel" not in raw
    start, raw = http(app, "read/result", {"flow_id": signed["flow_id"]})
    assert start["status"] == 404 and b"contract_sentinel" not in raw


@pytest.mark.parametrize("change", ["attempt", "generation"])
def test_read_refuses_a_different_active_evidence_binding(contract_read, change):
    app, _, signer, reads = contract_read
    signed = proof(app, signer, "read")
    setattr(reads, change, "d" * 32)
    start, raw = http(app, "read/finish", signed)
    assert start["status"] == 409 and json.loads(raw)["error"]["code"] == "generation_not_approved"


@pytest.mark.parametrize(
    "change", ["generation", "expiry", "owner_revoked", "patient_revoked", "serving_disabled", "proof_expired"]
)
def test_read_return_boundary_withholds_contract_body_after_change(contract_read, change):
    from src.interop.epic.beta_live_staging import LiveAllowlist

    app, _, signer, reads = contract_read

    def mutate():
        if change == "generation":
            reads.generation = "d" * 32
        if change == "expiry":
            reads.expired = True
        if change == "owner_revoked":
            app._auth.revoke_owner()
        if change == "patient_revoked":
            reads.allowlist = LiveAllowlist({})
        if change == "serving_disabled":
            app._live_reads_approved = False
        if change == "proof_expired":
            app._auth.clock.now += 121

    start, raw = http(app, "read/finish", proof(app, signer, "read"), on_start=mutate)
    assert start["status"] == 200 and raw == b""


def test_binding_is_write_once_digest_pinned_and_reopens_only_its_genuine_store(owned, monkeypatch):
    from api import beta_owner_operator as operator

    app, store, _ = owned
    monkeypatch.setattr(operator, "Custody", storage.Custody)
    monkeypatch.setattr(operator, "private_path", storage.private_path)
    with pytest.raises(ValueError):
        operator.save_owner_binding(store)
    digest = operator.save_owner_binding(store, enabled=True)
    raw = (store.root / operator.BINDING).read_bytes()
    assert set(json.loads(raw)) == {"version", "origin", "purpose", "principal", "handle"}
    with pytest.raises(FileExistsError):
        operator.save_owner_binding(store, enabled=True)
    with pytest.raises(ValueError):
        operator.open_owner_binding(store.root, "f" * 64, enabled=True, policy_accepted=True)
    store.close()
    reopened = operator.open_owner_binding(store.root, digest, enabled=True, policy_accepted=True)
    assert reopened.policy == app.policy and reopened.load().credential is None
    reopened.close()


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("action", ["enroll", "revoke"])
def test_same_loop_operator_requires_explicit_approval_and_never_runs_a_listener(
    owned, monkeypatch, capsys, approved, action
):
    from types import SimpleNamespace
    import uvicorn
    from api import beta_owner_operator as operator

    app, store, _ = owned
    app._action("enroll/pending", {"binding": BROWSER})
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    monkeypatch.setattr(genuine, "confirm_owner_action", lambda *args, **kwargs: True)
    commands = iter([action, "stop"])
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: next(commands))
    seen = []

    class NoListenerServer:
        should_exit = False

        def __init__(self, config):
            seen.append("constructed")

        async def serve(self):
            seen.append("stub only")
            while not self.should_exit:
                await asyncio.sleep(0.001)

    monkeypatch.setattr(uvicorn, "Server", NoListenerServer)
    config = SimpleNamespace(
        app=app,
        loaded_app=app,
        host="127.0.0.1",
        port=8443,
        workers=1,
        reload=False,
        proxy_headers=False,
        access_log=False,
        ssl=object(),
        log_level="warning",
    )
    with pytest.raises(ValueError):
        asyncio.run(operator.serve_owner_auth(app, config), loop_factory=_MemoryLoop)
    assert not seen
    approvals = []
    method = "approve_enrollment" if action == "enroll" else "revoke_owner"
    original = getattr(app, method)
    monkeypatch.setattr(app, method, lambda *args, **kwargs: approvals.append(1) or original(*args, **kwargs))
    asyncio.run(
        operator.serve_owner_auth(
            app, config, enabled=True, enrollment_approved=approved, revocation_approved=approved
        ),
        loop_factory=_MemoryLoop,
    )
    assert len(approvals) == int(approved) and seen == ["constructed", "stub only"]
    revoked = action == "revoke" and approved
    assert app._auth._revoked is revoked
    assert ("Owner credential permanently revoked." in capsys.readouterr().out) is revoked
    assert not app._auth._grants and not app._enrollments and not app._auth.sessions


class FakeConsoleKeys:
    """Pure fake msvcrt: scripted keystrokes in, echoed characters out. No OS console is touched."""

    def __init__(self, keys):
        self.keys, self.echoed = list(keys), []

    def kbhit(self):
        return bool(self.keys)

    def getwch(self):
        return self.keys.pop(0)

    def putwch(self, char):
        self.echoed.append(char)


def test_operator_console_echoes_commands_without_changing_acceptance(monkeypatch):
    import sys
    from api import beta_owner_operator as operator

    def poll(keys):
        fake = FakeConsoleKeys(keys)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        buffer, results = [], []
        while fake.keys:
            results.append(operator._poll_command(buffer))
        return results, "".join(fake.echoed), buffer

    monkeypatch.setattr(operator, "_local_console", lambda: True)
    assert poll("enroll\r") == (["enroll"], "enroll\r\n", [])
    assert poll("stoq\bp\r") == (["stop"], "stoq\b \bp\r\n", [])
    cleared = " [cleared]\r\n" + operator.COMMAND_PROMPT
    assert poll("en roll\r") == (["roll"], "en" + cleared + "roll\r\n", [])  # unchanged: invalid key clears
    assert poll("ENROLL\r") == ([""], cleared * 6 + "\r\n", [])  # unchanged: lowercase only
    assert poll("revokeabc\r") == ([""], "revokeab" + cleared + "\r\n", [])  # unchanged: 8-letter limit clears
    assert poll("\xe0Hstop\r") == (["stop"], cleared + "stop\r\n", [])  # special key consumed and cleared
    assert poll("\b\bstop") == ([None], "stop", list("stop"))  # backspace on empty echoes nothing; no Enter yet
    monkeypatch.setattr(operator, "_local_console", lambda: False)
    monkeypatch.setitem(sys.modules, "msvcrt", FakeConsoleKeys("stop\r"))
    with pytest.raises(ValueError):
        operator._poll_command([])


def test_operator_loop_shows_prompt_and_reports_unknown_commands(owned, monkeypatch, capsys):
    from types import SimpleNamespace
    import uvicorn
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    commands = iter(["bogus", "", None, "enroll", "stop"])
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: next(commands))

    class NoListenerServer:
        should_exit = False

        def __init__(self, config):
            pass

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.001)

    monkeypatch.setattr(uvicorn, "Server", NoListenerServer)
    config = SimpleNamespace(
        app=app,
        loaded_app=app,
        host="127.0.0.1",
        port=8443,
        workers=1,
        reload=False,
        proxy_headers=False,
        access_log=False,
        ssl=object(),
        log_level="warning",
    )
    asyncio.run(
        operator.serve_owner_auth(app, config, enabled=True, enrollment_approved=False), loop_factory=_MemoryLoop
    )
    out = capsys.readouterr().out
    assert out.count(operator.COMMAND_PROMPT) == 4  # start, after bogus, after empty, after enroll; not after stop
    assert out.count("Unknown command. Type enroll, revoke or stop.") == 1
    assert "Enrollment is not approved." in out and not app._enrollments and not app._auth._grants


@pytest.mark.parametrize("case", ["approved", "expired", "console_declined"])
def test_operator_enroll_reports_time_left_and_refusal_reason_without_changing_authority(
    owned, monkeypatch, capsys, case
):
    from types import SimpleNamespace
    import uvicorn
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    pending = app._action("enroll/pending", {"binding": BROWSER})
    if case == "expired":
        app._auth.clock.now += 61  # Enter pressed after the unchanged 60-second browser-request window
    else:
        app._auth.clock.now += 15
    prompts = []
    monkeypatch.setattr(
        genuine,
        "confirm_owner_action",
        lambda principal, fingerprint, **kwargs: prompts.append(fingerprint) or case == "approved",
    )
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    commands = iter(["enroll", "stop"])
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: next(commands))
    grants = []

    class NoListenerServer:
        should_exit = False

        def __init__(self, config):
            pass

        async def serve(self):
            while not self.should_exit:
                grants.extend(entry.grant for entry in app._enrollments.values() if entry.grant)
                await asyncio.sleep(0.001)

    monkeypatch.setattr(uvicorn, "Server", NoListenerServer)
    config = SimpleNamespace(
        app=app,
        loaded_app=app,
        host="127.0.0.1",
        port=8443,
        workers=1,
        reload=False,
        proxy_headers=False,
        access_log=False,
        ssl=object(),
        log_level="warning",
    )
    asyncio.run(
        operator.serve_owner_auth(app, config, enabled=True, enrollment_approved=True), loop_factory=_MemoryLoop
    )
    out = capsys.readouterr().out
    if case == "expired":
        assert "Browser enrollment request: 0 of 60 seconds left, including Windows Hello." in out
        assert operator.ENROLLMENT_REFUSALS["enrollment_rejected"] + " [enrollment_rejected]" in out
        assert prompts == [] and not grants  # expiry is still enforced before any console confirmation
    elif case == "console_declined":
        assert "Browser enrollment request: 45 of 60 seconds left, including Windows Hello." in out
        assert operator.ENROLLMENT_REFUSALS["enrollment_not_authorized"] + " [enrollment_not_authorized]" in out
        assert prompts == [pending["fingerprint"]] and not grants
    else:
        assert "Browser enrollment request: 45 of 60 seconds left, including Windows Hello." in out
        assert "Internal enrollment approved for that browser request only." in out
        assert prompts == [pending["fingerprint"]] and grants
    assert pending["fingerprint"] not in out  # the operator itself never prints the fingerprint
    assert not app._enrollments and not app._auth._grants and not app._auth.sessions


@pytest.mark.parametrize("case", ["request_arrives", "arming_expires", "stop_while_armed", "not_approved"])
def test_operator_enroll_arms_before_the_browser_request_without_changing_authority(owned, monkeypatch, capsys, case):
    from types import SimpleNamespace
    import uvicorn
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    prompts, grants, created = [], [], []
    monkeypatch.setattr(
        genuine, "confirm_owner_action", lambda principal, fingerprint, **kwargs: prompts.append(fingerprint) or True
    )
    monkeypatch.setattr(operator, "_local_console", lambda: True)

    def script():
        yield "enroll"  # armed: no browser request exists yet
        yield None
        if case == "request_arrives":
            created.append(app._action("enroll/pending", {"binding": BROWSER}))
            yield None  # same instant: within the response margin, the console must not prompt yet
            app._auth.clock.now += 2
            yield None  # now the console prompts for that one request
        elif case == "arming_expires":
            app._auth.clock.now += operator.ARM_S + 1
            yield None
            created.append(app._action("enroll/pending", {"binding": BROWSER}))
            app._auth.clock.now += 2
            yield None  # a request after arming expired must not prompt
        while True:
            yield "stop"

    commands = script()
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: next(commands))

    class NoListenerServer:
        should_exit = False

        def __init__(self, config):
            pass

        async def serve(self):
            while not self.should_exit:
                grants.extend(entry.grant for entry in app._enrollments.values() if entry.grant)
                await asyncio.sleep(0.001)

    monkeypatch.setattr(uvicorn, "Server", NoListenerServer)
    config = SimpleNamespace(
        app=app,
        loaded_app=app,
        host="127.0.0.1",
        port=8443,
        workers=1,
        reload=False,
        proxy_headers=False,
        access_log=False,
        ssl=object(),
        log_level="warning",
    )
    asyncio.run(
        operator.serve_owner_auth(app, config, enabled=True, enrollment_approved=case != "not_approved"),
        loop_factory=_MemoryLoop,
    )
    out = capsys.readouterr().out
    if case == "not_approved":
        assert "Enrollment is not approved." in out and "armed" not in out and not prompts
    else:
        assert f"Enrollment armed for {operator.ARM_S} seconds." in out
    if case == "request_arrives":
        assert "Browser enrollment request received." in out
        assert "Browser enrollment request: 58 of 60 seconds left, including Windows Hello." in out
        assert prompts == [created[0]["fingerprint"]] and grants
        assert "Internal enrollment approved for that browser request only." in out
    else:
        assert not prompts and not grants and "Internal enrollment approved" not in out
    if case == "arming_expires":
        assert "Enrollment arming expired; type enroll again when ready." in out
    assert all(item["fingerprint"] not in out for item in created)
    assert not app._enrollments and not app._auth.sessions


@pytest.mark.parametrize("case", ["default", "incomplete", "relative", "live_flag", "abbreviated"])
def test_auth_launcher_is_closed_and_default_denied_before_application_execution(monkeypatch, tmp_path, case):
    from scripts import run_owner_genuine as launcher

    calls = []
    monkeypatch.setattr(launcher, "_serve", lambda args: calls.append(args))
    arguments = {
        "default": [],
        "incomplete": ["--serve-owner-auth"],
        "relative": ["--serve-owner-auth", "--accept-owner-policy", "--binding-root", "relative"],
        "live_flag": ["--activate-live"],
        "abbreviated": ["--serve-owner"],
    }[case]
    assert launcher.main(arguments) in {1, 2} and not calls


def test_auth_launcher_passes_explicit_auth_only_inputs_to_stub_once(monkeypatch, tmp_path):
    from scripts import run_owner_genuine as launcher

    calls = []
    monkeypatch.setattr(launcher, "_serve", lambda args: calls.append(args))
    assert (
        launcher.main(
            [
                "--serve-owner-auth",
                "--accept-owner-policy",
                "--binding-root",
                str(tmp_path),
                "--binding-sha256",
                "a" * 64,
                "--certificate",
                str(tmp_path / "not-read.pem"),
                "--private-key",
                str(tmp_path / "not-read.key"),
                "--assets",
                str(tmp_path / "not-read-assets"),
            ]
        )
        == 0
    )
    assert len(calls) == 1 and not calls[0].allow_enrollment and not calls[0].allow_revocation


def _stub_listener(app, monkeypatch, *, record=None):
    from types import SimpleNamespace
    import uvicorn

    class NoListenerServer:
        should_exit = False

        def __init__(self, config):
            pass

        async def serve(self):
            while not self.should_exit:
                if record is not None:
                    record()
                await asyncio.sleep(0.001)

    monkeypatch.setattr(uvicorn, "Server", NoListenerServer)
    return SimpleNamespace(
        app=app,
        loaded_app=app,
        host="127.0.0.1",
        port=8443,
        workers=1,
        reload=False,
        proxy_headers=False,
        access_log=False,
        ssl=object(),
        log_level="warning",
    )


def test_refused_revocation_confirmation_keeps_the_listener_and_revokes_nothing(owned, monkeypatch, capsys):
    # Run 7 (2026-11-13): an inexact revoke confirmation ended the whole listener with the generic launcher error.
    from api import beta_owner_operator as operator

    app, store, _ = owned
    answers = iter([False, True])
    prompts = []
    monkeypatch.setattr(
        genuine,
        "confirm_owner_action",
        lambda principal, fingerprint, **kwargs: prompts.append(fingerprint) or next(answers),
    )
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    states = []
    commands = iter(["revoke", None, "revoke"])

    def poll(buffer):
        states.append(app._auth._revoked)
        return next(commands, None)

    monkeypatch.setattr(operator, "_poll_command", poll)
    config = _stub_listener(app, monkeypatch)
    asyncio.run(
        operator.serve_owner_auth(app, config, enabled=True, revocation_approved=True), loop_factory=_MemoryLoop
    )
    out = capsys.readouterr().out
    assert operator.REVOCATION_REFUSALS["revocation_not_authorized"] + " [revocation_not_authorized]" in out
    assert states[:3] == [False, False, False]  # nothing revoked after the refusal; listener kept polling
    assert len(prompts) == 2 and prompts[0] == prompts[1]  # each attempt is a fresh, full console confirmation
    assert out.count("Owner credential permanently revoked.") == 1 and app._auth._revoked
    assert prompts[0] not in out


def test_revocation_storage_failure_still_ends_the_run(owned, monkeypatch):
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: "revoke")

    def failing(*, enabled=False):
        raise RefreshRejected(503, "owner_storage_unavailable")

    monkeypatch.setattr(app, "revoke_owner", failing)
    config = _stub_listener(app, monkeypatch)
    with pytest.raises(RefreshRejected):
        asyncio.run(
            operator.serve_owner_auth(app, config, enabled=True, revocation_approved=True), loop_factory=_MemoryLoop
        )
    assert not app._auth._grants and not app._enrollments


def test_bounded_run_warns_then_stops_without_approving_anything(owned, monkeypatch, capsys):
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    app._action("enroll/pending", {"binding": BROWSER})
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    monkeypatch.setattr(genuine, "confirm_owner_action", lambda *args, **kwargs: pytest.fail("no confirmation"))
    clock = iter([0.0, 10.0, 545.0, 546.0, 599.0, 600.0, 601.0])
    monkeypatch.setattr(operator, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: None)
    config = _stub_listener(app, monkeypatch)
    limited = asyncio.run(
        operator.serve_owner_auth(
            app, config, enabled=True, enrollment_approved=True, revocation_approved=True, stop_after_s=600
        ),
        loop_factory=_MemoryLoop,
    )
    out = capsys.readouterr().out
    assert limited is True
    assert "This listener stops by itself after 10 minutes." in out
    assert out.count("One minute left before this listener stops.") == 1
    assert "Run limit reached; the listener is stopping." in out
    assert not app._auth._revoked and not app._auth._grants and not app._enrollments


@pytest.mark.parametrize("bound", [0, -1, 601, True, "600", float("nan")])
def test_run_bound_must_be_an_explicit_positive_ten_minute_limit(owned, monkeypatch, bound):
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    config = _stub_listener(app, monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(operator.serve_owner_auth(app, config, enabled=True, stop_after_s=bound), loop_factory=_MemoryLoop)


def test_auth_launcher_always_passes_the_ten_minute_bound(monkeypatch, tmp_path):
    from api import beta_owner_genuine as genuine_module
    from api import beta_owner_operator as operator
    from scripts import run_owner_genuine as launcher
    from scripts import run_owner_https_diagnostic as diagnostic

    seen = {}

    class Store:
        def close(self):
            seen["closed"] = True

    async def serve(app, config, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(diagnostic, "_lock_environment", lambda: None)
    monkeypatch.setattr(operator, "open_owner_binding", lambda *args, **kwargs: Store())
    monkeypatch.setattr(operator, "serve_owner_auth", serve)
    monkeypatch.setattr(genuine_module, "create_genuine_owner_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(genuine_module, "prepare_genuine_owner_https", lambda *args, **kwargs: object())
    args = [
        "--serve-owner-auth",
        "--accept-owner-policy",
        "--binding-root",
        str(tmp_path),
        "--binding-sha256",
        "a" * 64,
        "--certificate",
        str(tmp_path / "c.pem"),
        "--private-key",
        str(tmp_path / "k.pem"),
        "--assets",
        str(tmp_path / "assets"),
        "--allow-revocation",
    ]
    assert launcher.main(args) == 0
    assert seen == {
        "enabled": True,
        "enrollment_approved": False,
        "revocation_approved": True,
        "stop_after_s": launcher.RUN_LIMIT_S,
        "closed": True,
    }
    assert launcher.RUN_LIMIT_S == 600


class FakeTimer:
    made: list["FakeTimer"] = []

    def __init__(self, interval, function):
        self.interval, self.function = interval, function
        self.daemon = self.started = self.cancelled = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def test_unanswered_confirmation_cannot_outlast_the_bound(owned, monkeypatch, capsys):
    # A console confirmation blocks the loop in input(); the independent timer must end the process anyway.
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    FakeTimer.made = []
    monkeypatch.setattr(operator.threading, "Timer", FakeTimer)
    exits = []

    class HardExit(BaseException):
        pass

    def hard_exit(code):
        exits.append(code)
        raise HardExit

    monkeypatch.setattr(operator, "_hard_exit", hard_exit)

    def blocked_confirmation(*args, **kwargs):
        FakeTimer.made[0].function()  # the timer fires while the owner never answers
        return True  # unreachable: the process has ended

    monkeypatch.setattr(genuine, "confirm_owner_action", blocked_confirmation)
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: "revoke")
    config = _stub_listener(app, monkeypatch)
    with pytest.raises(HardExit):
        asyncio.run(
            operator.serve_owner_auth(app, config, enabled=True, revocation_approved=True, stop_after_s=600),
            loop_factory=_MemoryLoop,
        )
    timer = FakeTimer.made[0]
    assert timer.interval == 600 + operator.HARD_STOP_GRACE_S and timer.daemon and timer.started
    assert exits == [operator.HARD_STOP_EXIT] == [3]
    assert "Run limit passed while waiting for console input" in capsys.readouterr().out
    assert not app._auth._revoked and not app._auth._grants


def test_orderly_stop_cancels_the_hard_stop_timer(owned, monkeypatch):
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    FakeTimer.made = []
    monkeypatch.setattr(operator.threading, "Timer", FakeTimer)
    monkeypatch.setattr(operator, "_hard_exit", lambda code: pytest.fail("hard stop after an orderly stop"))
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: "stop")
    config = _stub_listener(app, monkeypatch)
    limited = asyncio.run(
        operator.serve_owner_auth(app, config, enabled=True, stop_after_s=600), loop_factory=_MemoryLoop
    )
    assert limited is False and FakeTimer.made[0].started and FakeTimer.made[0].cancelled


def test_unbounded_legacy_calls_create_no_hard_stop_timer(owned, monkeypatch):
    from api import beta_owner_operator as operator

    app, _store, _ = owned
    FakeTimer.made = []
    monkeypatch.setattr(operator.threading, "Timer", FakeTimer)
    monkeypatch.setattr(operator, "_local_console", lambda: True)
    monkeypatch.setattr(operator, "_poll_command", lambda buffer: "stop")
    config = _stub_listener(app, monkeypatch)
    assert asyncio.run(operator.serve_owner_auth(app, config, enabled=True), loop_factory=_MemoryLoop) is False
    assert FakeTimer.made == []


def test_auth_launcher_reports_the_run_limit_with_its_own_exit_code(monkeypatch, tmp_path):
    from scripts import run_owner_genuine as launcher

    monkeypatch.setattr(launcher, "_serve", lambda args: True)
    args = [
        "--serve-owner-auth",
        "--accept-owner-policy",
        "--binding-root",
        str(tmp_path),
        "--binding-sha256",
        "a" * 64,
    ]
    args += [
        "--certificate",
        str(tmp_path / "c"),
        "--private-key",
        str(tmp_path / "k"),
        "--assets",
        str(tmp_path / "a"),
    ]
    assert launcher.main(args) == launcher.RUN_LIMIT_EXIT == 3
    monkeypatch.setattr(launcher, "_serve", lambda args: False)
    assert launcher.main(args) == 0
