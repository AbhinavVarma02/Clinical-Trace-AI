"""Offline browser-bootstrap contracts; no browser, certificates or network providers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
from pathlib import Path
import stat
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_beta_owner_refresh import ALPHA, BRAVO, ORIGIN, harness as owner_fixture, keys as keys_fixture

harness = owner_fixture
keys = keys_fixture

SESSION = "/beta/auth/session"
PATIENTS = "/beta/epic/patients"
READ = {
    "X-Beta-Client": "browser-v1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "same-origin",
    "Sec-Fetch-Dest": "empty",
    "Accept": "application/json",
}
FORM = {
    "Origin": ORIGIN,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Accept": "text/html",
}
CSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self' https://owner-idp.invalid; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"


def require(condition, code):
    """Do not include authentication values or response bodies in failure output."""
    if not condition:
        raise AssertionError(code)


def denied(response, status, code):
    require(response.status_code == status, f"expected-status-{status}-received-{response.status_code}")
    if response.request.method != "HEAD":
        require(response.json() == {"error": {"code": code}}, "safe-error-envelope")
    require(response.headers.get("cache-control") == "no-store", "no-store-error")
    require("access-control-allow-origin" not in response.headers, "no-cors")


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "invented-build"
    (root / ".vite").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "index.html").write_text(
        '<!doctype html><html><head><title>MOCK Beta</title><link rel="stylesheet" href="/beta/app/assets/main-A.css"></head><body><div id="root"></div><script type="module" src="/beta/app/assets/main-A.js"></script></body></html>',
        encoding="utf-8",
    )
    (root / "assets/main-A.js").write_text("import './shared-A.js'; export const mock = true;", encoding="utf-8")
    (root / "assets/shared-A.js").write_text("export const invented = 1;", encoding="utf-8")
    (root / "assets/main-A.css").write_text("body { color: #123; }", encoding="utf-8")
    (root / ".vite/manifest.json").write_text(
        json.dumps(
            {
                "index.html": {
                    "file": "assets/main-A.js",
                    "src": "index.html",
                    "isEntry": True,
                    "css": ["assets/main-A.css"],
                    "imports": ["_shared-A.js"],
                },
                "_shared-A.js": {"file": "assets/shared-A.js"},
            }
        ),
        encoding="utf-8",
    )
    return root


def make_app(h, signing_keys, root):
    from api.beta import create_mock_beta_app

    return create_mock_beta_app(
        h.db,
        oidc_key=signing_keys[0].public_key(),
        epic_key=signing_keys[1],
        oidc_transport=httpx.MockTransport(h.oidc),
        epic_transport=httpx.MockTransport(h.epic),
        clock=lambda: h.clock,
        browser_assets_root=root,
    )


@pytest.fixture
def browser(harness, keys, assets):
    return harness, TestClient(make_app(harness, keys, assets), base_url=ORIGIN, follow_redirects=False)


def begin_browser(h, client):
    response = client.post("/beta/auth/browser-login", headers=FORM)
    require(response.status_code == 303, "browser-start")
    target = urlsplit(response.headers["location"])
    require(f"{target.scheme}://{target.netloc}{target.path}" == "https://owner-idp.invalid/authorize", "fixed-idp")
    h.auth_request = parse_qs(target.query)
    return h.auth_request["state"][0]


def test_factory_accepts_explicit_validated_assets(harness, keys, assets):
    with TestClient(make_app(harness, keys, assets), base_url=ORIGIN) as client:
        require(client.get("/beta/app/").status_code == 200, "explicit-shell")
        denied(client.get(SESSION, headers=READ), 401, "owner_required")
    require(not harness.calls and not harness.oidc_calls, "no-transports")


def test_default_factory_keeps_browser_and_legacy_routes_closed(harness):
    with harness.client() as client:
        harness.login(client)
        for path in (SESSION, PATIENTS, "/beta/auth/browser-login", "/beta/app/", "/app/", "/api/v2/patients"):
            require(client.get(path).status_code == 404, "default-no-browser-route")


def test_legacy_and_browser_callback_compatibility_and_rotation(browser):
    h, context = browser
    with context as client:
        h.login(client)
        previous = client.cookies.get("__Host-beta-session")
        old_csrf = h.csrf
        state = begin_browser(h, client)
        response = h.finish(client, state)
        require(
            response.status_code == 303 and response.headers["location"] == "/beta/app/" and not response.content,
            "fixed-empty-return",
        )
        require(client.cookies.get("__Host-beta-session") != previous, "session-rotated")
        for part in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
            require(part in response.headers["set-cookie"], "cookie-attributes")
        require("Domain=" not in response.headers["set-cookie"], "host-cookie")
        current = client.get(SESSION, headers=READ)
        require(current.status_code == 200 and current.json()["csrf_token"] != old_csrf, "csrf-rotated")
        denied(
            client.get(SESSION, headers={**READ, "Cookie": f"__Host-beta-session={previous}"}), 401, "owner_required"
        )
        denied(h.finish(client, state), 401, "login_rejected")
        h.login(client)
        require(client.get(SESSION, headers=READ).status_code == 200, "legacy-restored")
    require(len(h.oidc_calls) == 3 and not h.calls, "only-mock-auth")


def test_session_shape_and_passive_patients(browser):
    h, context = browser
    with context as client:
        h.login(client)
        created = h.clock
        cookie = client.cookies.get("__Host-beta-session")
        response = client.get(SESSION, headers=READ)
        require(response.status_code == 200, "session-success")
        value = response.json()
        require(
            set(value)
            == {
                "authenticated",
                "mode",
                "owner_label",
                "csrf_token",
                "server_time_unix_s",
                "created_at_unix_s",
                "last_activity_at_unix_s",
                "idle_expires_at_unix_s",
                "absolute_expires_at_unix_s",
            },
            "session-field-set",
        )
        require(
            value["csrf_token"] == h.csrf and value["authenticated"] is True and value["mode"] == "MOCK",
            "session-values",
        )
        require(value["owner_label"] == "Authenticated MOCK owner", "safe-owner-label")
        require(value["created_at_unix_s"] == value["last_activity_at_unix_s"] == created, "activity-origin")
        require(
            value["idle_expires_at_unix_s"] == created + 900 and value["absolute_expires_at_unix_s"] == created + 28800,
            "unchanged-limits",
        )
        require(
            "set-cookie" not in response.headers and cookie not in response.text, "no-cookie-disclosure-or-rotation"
        )
        require(response.headers.get("cross-origin-resource-policy") == "same-origin", "same-origin-corp")
        require(
            response.headers.get("referrer-policy") == "no-referrer"
            and response.headers.get("x-content-type-options") == "nosniff",
            "privacy-headers",
        )
        expected = [{"alias": "mock-alpha", "label": "MOCK Alpha"}, {"alias": "mock-bravo", "label": "MOCK Bravo"}]
        require(
            client.get(PATIENTS, headers=READ).json()
            == {"mode": "MOCK", "source": "epic-sandbox-mock", "patients": expected},
            "safe-aliases",
        )
        require(h.post(client, ALPHA + "/revoke").status_code == 204, "revoke-alpha")
        require(client.get(PATIENTS, headers=READ).json()["patients"] == expected[1:], "revoked-filter")
        denied(client.get(ALPHA + "/status"), 404, "patient_unavailable")
        require(h.post(client, BRAVO + "/revoke").status_code == 204, "revoke-bravo")
        require(client.get(PATIENTS, headers=READ).json()["patients"] == [], "empty-approved-list")
    require(len(h.oidc_calls) == 1 and not h.calls, "reads-no-transport")


@pytest.mark.parametrize("path", [SESSION, PATIENTS])
@pytest.mark.parametrize("name", list(READ))
@pytest.mark.parametrize("attack", ["missing", "duplicate", "wrong"])
def test_protected_read_browser_header_profile(browser, path, name, attack):
    h, context = browser
    with context as client:
        h.login(client)
        headers = [(k, v) for k, v in READ.items() if k != name]
        if attack == "duplicate":
            headers += [(name, READ[name]), (name.lower(), READ[name])]
        elif attack == "wrong":
            headers.append((name, "cross-site"))
        denied(client.get(path, headers=headers), 403, "same_origin_required")
    require(not h.calls, "profile-no-provider")


@pytest.mark.parametrize("path", [SESSION, PATIENTS])
@pytest.mark.parametrize(
    "attack",
    [
        "origin",
        "null-origin",
        "duplicate-origin",
        "query",
        "body",
        "post",
        "head",
        "options",
        "duplicate-cookie",
        "forged-cookie",
    ],
)
def test_bootstrap_request_boundaries(browser, path, attack):
    h, context = browser
    with context as client:
        h.login(client)
        headers = list(READ.items())
        method, target, content, status, code = "GET", path, None, 403, "same_origin_required"
        if attack in {"origin", "null-origin"}:
            headers.append(("Origin", "null" if attack == "null-origin" else "https://evil.invalid"))
        elif attack == "duplicate-origin":
            headers += [("Origin", ORIGIN), ("origin", ORIGIN)]
        elif attack == "query":
            target, status, code = path + "?next=secret", 400, "unexpected_payload"
        elif attack == "body":
            content, status, code = b"{}", 400, "unexpected_payload"
        elif attack in {"post", "head", "options"}:
            method, status, code = attack.upper(), 405, "method_not_allowed"
        elif attack == "duplicate-cookie":
            token = client.cookies.get("__Host-beta-session")
            headers.append(("Cookie", f"__Host-beta-session={token}; __Host-beta-session={token}"))
            status, code = 401, "owner_required"
        elif attack == "forged-cookie":
            headers.append(("Cookie", "__Host-beta-session=" + "a" * 43))
            status, code = 401, "owner_required"
        denied(client.request(method, target, headers=headers, content=content), status, code)
    require(not h.calls, "boundary-no-provider")


@pytest.mark.parametrize(
    "header,value,status,code",
    [
        ("Host", "beta.invalid:443", 403, "invalid_origin"),
        ("Host", "beta.invalid.evil.invalid", 403, "invalid_origin"),
        ("X-Demo-Persona", "admin.demo", 400, "untrusted_header"),
        ("X-Forwarded-Host", "beta.invalid", 400, "untrusted_header"),
        ("Authorization", "Bearer invented", 400, "untrusted_header"),
        ("X-Data-Source", "synthetic", 400, "untrusted_header"),
    ],
)
def test_browser_preserves_origin_and_spoofing_boundary(browser, header, value, status, code):
    h, context = browser
    with context as client:
        for path in ("/beta/app/", "/beta/auth/browser-login"):
            denied(client.get(path, headers={header: value}), status, code)
        h.login(client)
        denied(client.get(SESSION, headers={**READ, header: value}), status, code)
        denied(client.get("http://beta.invalid/beta/app/"), 403, "invalid_origin")


@pytest.mark.parametrize(
    "attack", ["origin", "missing-origin", "site", "mode", "dest", "accept", "query", "body", "duplicate"]
)
def test_browser_login_shape(browser, attack):
    h, context = browser
    with context as client:
        headers, target, content = list(FORM.items()), "/beta/auth/browser-login", None
        status, code = 403, "same_origin_required"
        field = {
            "origin": "Origin",
            "missing-origin": "Origin",
            "site": "Sec-Fetch-Site",
            "mode": "Sec-Fetch-Mode",
            "dest": "Sec-Fetch-Dest",
            "accept": "Accept",
        }.get(attack)
        if field:
            headers = [(k, v) for k, v in headers if k != field]
            if attack != "missing-origin":
                headers.append((field, "https://evil.invalid" if attack == "origin" else "invalid"))
            if field == "Origin":
                code = "invalid_origin"
        elif attack == "query":
            target += "?channel=legacy-json"
            status, code = 400, "unexpected_payload"
        elif attack == "body":
            content, status, code = b"next=https://evil.invalid", 400, "unexpected_payload"
        else:
            headers.append(("Sec-Fetch-Site", "same-origin"))
        denied(client.post(target, headers=headers, content=content), status, code)
    require(not h.oidc_calls and not h.calls, "invalid-login-no-transports")


@pytest.mark.parametrize(
    "attack",
    ["nonce", "signature", "state", "expired", "switched-flow", "extra-query", "duplicate-state", "unknown-channel"],
)
def test_browser_flow_forgery_replay_and_channel_binding(browser, monkeypatch, keys, attack):
    from api.beta_oidc import OwnerAuth

    h, context = browser
    if attack == "unknown-channel":
        original = OwnerAuth.begin

        def corrupt(self, *args, **kwargs):
            token, url = original(self, *args, **kwargs)
            self.flows[token] = replace(self.flows[token], channel="untrusted-channel")
            return token, url

        monkeypatch.setattr(OwnerAuth, "begin", corrupt)
    with context as client:
        state = begin_browser(h, client)
        if attack == "nonce":
            h.claims["nonce"] = "wrong"
        elif attack == "signature":
            h.signer = keys[1]
        elif attack == "state":
            state = "z" * 43
        elif attack == "expired":
            h.clock += 300
        elif attack == "switched-flow":
            h.begin(client)
        query = [("state", state), ("code", "invented-code")]
        if attack == "extra-query":
            query.append(("channel", "browser-shell"))
        elif attack == "duplicate-state":
            query = [("state", state), ("state", state)]
        denied(client.get("/beta/auth/callback", params=query), 401, "login_rejected")
        denied(client.get(SESSION, headers=READ), 401, "owner_required")
    require(not h.calls, "no-epic-on-forgery")


def test_legacy_flow_cannot_be_switched_by_browser_headers(browser):
    h, context = browser
    with context as client:
        state = h.begin(client)
        response = client.get(
            "/beta/auth/callback",
            params={"state": state, "code": "invented-code"},
            headers={**FORM, "X-Beta-Client": "browser-v1"},
        )
        require(response.status_code == 200 and set(response.json()) == {"csrf_token"}, "legacy-channel-fixed")


def test_static_manifest_head_csp_and_immutable_bytes(browser, assets):
    h, context = browser
    with context as client:
        for path, mime in [
            ("/beta/app/", "text/html"),
            ("/beta/app/assets/main-A.js", "text/javascript"),
            ("/beta/app/assets/main-A.css", "text/css"),
            ("/beta/app/assets/shared-A.js", "text/javascript"),
        ]:
            response = client.get(path)
            require(response.status_code == 200 and response.headers["content-type"].startswith(mime), "asset-mime")
            require(response.headers.get("content-security-policy") == CSP, "exact-csp")
            require(response.headers.get("cross-origin-resource-policy") == "same-origin", "static-corp")
            require(
                response.headers.get("cache-control") == "no-store" and "etag" not in response.headers,
                "static-no-cache",
            )
            head = client.head(path)
            require(
                head.status_code == 200
                and not head.content
                and head.headers["content-type"] == response.headers["content-type"],
                "head-parity",
            )
        original = client.get("/beta/app/assets/main-A.js").content
        (assets / "assets/main-A.js").write_text("private replacement must never be returned", encoding="utf-8")
        (assets / "index.html").unlink()
        require(client.get("/beta/app/assets/main-A.js").content == original, "pinned-asset")
        require(client.get("/beta/app/").status_code == 200, "pinned-shell")
        require(client.get("/beta/app/", headers={"If-None-Match": "anything"}).status_code == 200, "no-304")
    require(not h.calls and not h.oidc_calls, "anonymous-static-only")


@pytest.mark.parametrize(
    "path",
    [
        "/beta/app",
        "/beta/app/index.html",
        "/beta/app/patients",
        "/beta/app/assets/unknown.js",
        "/beta/app/assets/main-A.js.map",
        "/beta/app/.vite/manifest.json",
        "/beta/app/.env",
        "/beta/app/assets/%2e%2e%2fsecret.js",
        "/beta/app/assets/main%2dA.js",
        "/beta/app//assets/main-A.js",
        "/beta/app/assets/..%5csecret.js",
    ],
)
def test_static_only_allowlisted_canonical_paths(browser, path):
    _, context = browser
    with context as client:
        denied(client.get(path), 404, "not_found")
        response = client.head(path)
        require(response.status_code == 404 and not response.content, "head-denial")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_static_namespace_never_mutates_or_falls_back(browser, method):
    _, context = browser
    with context as client:
        denied(client.request(method, "/beta/app/"), 405, "method_not_allowed")


@pytest.mark.parametrize("payload", ["query", "body"])
def test_static_rejects_payloads(browser, payload):
    _, context = browser
    with context as client:
        response = (
            client.get("/beta/app/?next=secret")
            if payload == "query"
            else client.request("GET", "/beta/app/", content=b"{}")
        )
        denied(response, 400, "unexpected_payload")


@pytest.mark.parametrize(
    "attack",
    [
        "missing-root",
        "missing-manifest",
        "missing-entry",
        "bad-json",
        "duplicate-json",
        "entry-escape",
        "map",
        "mime",
        "missing-asset",
        "unknown-import",
        "inline-script",
        "remote-script",
        "event-handler",
        "remote-css",
        "manifest-size",
        "asset-size",
        "reparse-root",
        "reparse-asset",
    ],
)
def test_unsafe_build_fails_closed(harness, keys, assets, monkeypatch, attack):
    manifest_path = assets / ".vite/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    root = assets
    if attack == "missing-root":
        root = assets / "missing"
    elif attack == "missing-manifest":
        manifest_path.unlink()
    elif attack == "missing-entry":
        manifest.pop("index.html")
    elif attack == "bad-json":
        manifest_path.write_text("not JSON", encoding="utf-8")
    elif attack == "duplicate-json":
        manifest_path.write_text('{"index.html":{},"index.html":{}}', encoding="utf-8")
    elif attack == "entry-escape":
        manifest["index.html"]["file"] = "../secret.js"
    elif attack in {"map", "mime"}:
        name = "assets/secret.js.map" if attack == "map" else "assets/secret.json"
        (assets / name).write_text("{}", encoding="utf-8")
        manifest["index.html"]["file"] = name
    elif attack == "missing-asset":
        (assets / "assets/main-A.js").unlink()
    elif attack == "unknown-import":
        manifest["index.html"]["imports"] = ["missing"]
    elif attack in {"inline-script", "remote-script", "event-handler"}:
        text = {
            "inline-script": "<script>window.secret=1</script>",
            "remote-script": '<script src="https://evil.invalid/x.js"></script>',
            "event-handler": '<body onload="alert(1)"></body>',
        }[attack]
        (assets / "index.html").write_text(text, encoding="utf-8")
    elif attack == "remote-css":
        (assets / "assets/main-A.css").write_text('@import "https://evil.invalid/style.css";', encoding="utf-8")
    elif attack == "manifest-size":
        manifest_path.write_bytes(b" " * (64 * 1024 + 1))
    elif attack == "asset-size":
        (assets / "assets/main-A.js").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    elif attack.startswith("reparse"):
        target = assets if attack == "reparse-root" else assets / "assets/main-A.js"
        original = Path.lstat

        def injected(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == target:
                return SimpleNamespace(
                    st_mode=result.st_mode, st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
                )
            return result

        monkeypatch.setattr(Path, "lstat", injected)
    if attack in {"missing-entry", "entry-escape", "map", "mime", "unknown-import"}:
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe browser assets"):
        make_app(harness, keys, root)
    require(not harness.calls and not harness.oidc_calls, "invalid-build-no-transports")


@pytest.mark.parametrize("boundary", ["logout", "expiry", "owner-revocation"])
def test_passive_read_rechecks_authorization_after_awaited_body(browser, boundary):
    h, context = browser
    with context as client:
        h.login(client)
        cookie = {"Cookie": f"__Host-beta-session={client.cookies.get('__Host-beta-session')}"}

        async def scenario():
            waiting, resume = asyncio.Event(), asyncio.Event()

            async def body():
                yield b""
                waiting.set()
                await resume.wait()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=client.app), base_url=ORIGIN, headers=cookie
            ) as concurrent:
                task = asyncio.create_task(concurrent.request("GET", SESSION, headers=READ, content=body()))
                await asyncio.wait_for(waiting.wait(), 1)
                try:
                    if boundary == "logout":
                        require(
                            (
                                await concurrent.post(
                                    "/beta/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": h.csrf}
                                )
                            ).status_code
                            == 204,
                            "concurrent-logout",
                        )
                    elif boundary == "expiry":
                        h.clock += 900
                    else:
                        await client.app.state.revoke_owner()
                finally:
                    resume.set()
                return await task

        denied(client.portal.call(scenario), 401, "owner_required")
    require(not h.calls, "async-read-no-acquisition")


def test_bootstrap_errors_and_application_logs_are_content_free(browser, caplog):
    h, context = browser
    caplog.set_level(logging.WARNING, logger="httpx")
    with context as client:
        h.login(client)
        sentinels = [h.csrf, client.cookies.get("__Host-beta-session"), "invented-clinical-private-sentinel"]
        response = client.request("GET", SESSION, headers=READ, content=sentinels[-1].encode())
        denied(response, 400, "unexpected_payload")
        require(
            all(value not in response.text and value not in caplog.text for value in sentinels),
            "sensitive-log-or-error",
        )
    require(not h.calls, "privacy-no-epic")


@pytest.mark.parametrize(
    "attack", ["srcset", "svg-mime", "file-count", "aggregate-size", "duplicate-host", "duplicate-length"]
)
def test_additional_static_and_header_boundaries(harness, keys, assets, attack):
    if attack in {"duplicate-host", "duplicate-length"}:
        with TestClient(make_app(harness, keys, assets), base_url=ORIGIN) as client:
            headers = (
                [("Host", "beta.invalid"), ("host", "beta.invalid")]
                if attack == "duplicate-host"
                else [("Content-Length", "0"), ("content-length", "0")]
            )
            denied(
                client.get("/beta/app/", headers=headers),
                403 if attack == "duplicate-host" else 400,
                "invalid_origin" if attack == "duplicate-host" else "unexpected_payload",
            )
        return
    manifest_path = assets / ".vite/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if attack == "srcset":
        with (assets / "index.html").open("a", encoding="utf-8") as stream:
            stream.write('<img srcset="https://evil.invalid/private.png 2x">')
    elif attack == "svg-mime":
        (assets / "assets/wrong.svg").write_text("plain private text is not an SVG image", encoding="utf-8")
        manifest["index.html"]["assets"] = ["assets/wrong.svg"]
    elif attack == "file-count":
        manifest["index.html"]["assets"] = [f"assets/extra-{i}.js" for i in range(128)]
        for name in manifest["index.html"]["assets"]:
            (assets / name).write_text("export {};", encoding="utf-8")
    else:
        names = [f"assets/large-{i}.js" for i in range(4)]
        for name in names:
            (assets / name).write_bytes(b" " * (4 * 1024 * 1024))
        manifest["index.html"]["assets"] = names
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe browser assets"):
        make_app(harness, keys, assets)


def test_bootstrap_internal_errors_are_sanitized(browser, monkeypatch, caplog):
    import api.beta as beta_module

    h, context = browser
    secret = "invented-private-internal-error-sentinel"

    def broken(_):
        raise RuntimeError(secret)

    with context as client:
        h.login(client)
        monkeypatch.setattr(beta_module, "patient_view", broken)
        response = client.get(PATIENTS, headers=READ)
        denied(response, 500, "internal_error")
        require(secret not in response.text and secret not in caplog.text, "sanitized-internal-error")


@pytest.mark.parametrize(
    "length", ["-1", "invalid", "1" * 5000, "\u0660"], ids=["negative", "text", "oversized-number", "unicode-zero"]
)
def test_payload_length_rejects_invalid_numbers_without_parsing_them(length):
    from api.beta_browser import payload_length
    from src.interop.epic.beta_catalog import RefreshRejected
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [(b"content-length", length.encode("utf-8"))]})
    with pytest.raises(RefreshRejected) as error:
        payload_length(request)
    require(error.value.status == 400 and error.value.code == "unexpected_payload", "invalid-length-safe-denial")


def test_browser_flow_checks_revocation_after_token_await(browser):
    h, context = browser
    original = h.oidc

    with context as client:
        state = begin_browser(h, client)
        # Change the transport's existing handler without introducing a network client.
        from api.beta_oidc import OwnerAuth

        owners = []
        original_complete = OwnerAuth.complete

        async def wrapped(owner, *args):
            owners.append(owner)
            transport = owner.transport
            prior = transport.handler

            async def revoke(request):
                result = original(request)
                await client.app.state.revoke_owner()
                return result

            transport.handler = revoke
            try:
                return await original_complete(owner, *args)
            finally:
                transport.handler = prior

        from unittest.mock import patch

        with patch.object(OwnerAuth, "complete", wrapped):
            denied(h.finish(client, state), 401, "login_rejected")
        require(not owners[0].sessions, "no-session-after-revocation")
    require(not h.calls, "revoked-auth-no-epic")
