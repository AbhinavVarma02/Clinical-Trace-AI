"""N2 launcher checks with synthetic inputs and stubbed serving only.

No test opens a listener, browser, real credential, OS identity or certificate store. The TLS/logging
case uses invented CA/leaf files and fake custody; it establishes neither OS nor browser acceptance.
"""

from __future__ import annotations

import asyncio
import ast
import builtins
import logging
from pathlib import Path

import pytest
import uvicorn
from uvicorn.middleware.message_logger import MessageLoggerMiddleware

from api import beta_owner_https as https
from api.beta_webauthn import OWNER_ORIGIN, OWNER_RP_ID, LocalOwnerPolicy
from scripts import run_owner_https_diagnostic as launcher
from tests.test_beta_owner_https import response as diagnostic_response
from tests.test_beta_owner_origin import dedicated_certificates as _dedicated_certificates
from tests.test_beta_synthetic_ui import _MemoryLoop
from tests.test_beta_webauthn import HANDLE


@pytest.fixture
def dedicated_certificates(tmp_path, monkeypatch):
    return _dedicated_certificates.__wrapped__(tmp_path, monkeypatch)


def opt_in(certificate: Path, key: Path) -> list[str]:
    return ["--serve-diagnostic", "--certificate", str(certificate), "--private-key", str(key)]


@pytest.mark.parametrize("case", ["default", "paths_without_enable", "missing_paths", "relative_paths"])
def test_default_or_incomplete_invocation_cannot_import_apps_read_paths_or_serve(monkeypatch, tmp_path, case):
    certificate, key = tmp_path / "not-opened.pem", tmp_path / "not-opened.key"
    arguments = {
        "default": [],
        "paths_without_enable": ["--certificate", str(certificate), "--private-key", str(key)],
        "missing_paths": ["--serve-diagnostic"],
        "relative_paths": opt_in(Path("unapproved.pem"), Path("unapproved.key")),
    }[case]
    original_import = builtins.__import__

    def no_app_import(name, *args, **kwargs):
        if name.startswith(("api", "uvicorn", "fastapi", "fido2", "cryptography")):
            pytest.fail("disabled launcher imported application code")
        return original_import(name, *args, **kwargs)

    def prohibited(*_args, **_kwargs):
        pytest.fail("disabled launcher accessed a path or serving seam")

    with monkeypatch.context() as blocked:
        blocked.setattr(builtins, "__import__", no_app_import)
        blocked.setattr(Path, "stat", prohibited)
        blocked.setattr(Path, "read_bytes", prohibited)
        blocked.setattr(Path, "open", prohibited)
        blocked.setattr(launcher, "_prepare", prohibited)
        blocked.setattr(launcher, "_serve", prohibited)
        assert launcher.main(arguments) == (2 if case in {"default", "paths_without_enable"} else 1)


@pytest.mark.parametrize(
    "extra",
    [
        ["--host", "0.0.0.0"],
        ["--port", "9443"],
        ["--origin", "https://unapproved.invalid"],
        ["--open"],
        ["--serve"],  # argparse abbreviations must not become a second activation spelling
    ],
)
def test_launcher_options_cannot_select_host_port_origin_browser_or_abbreviated_activation(
    monkeypatch, tmp_path, extra
):
    monkeypatch.setattr(launcher, "_serve", lambda *_: pytest.fail("unknown option enabled serving"))
    assert launcher.main(opt_in(tmp_path / "invented.pem", tmp_path / "invented.key") + extra) == 1


def test_invalid_command_line_never_echoes_unrecognized_sensitive_values(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "_serve", lambda *_: pytest.fail("invalid option enabled serving"))
    assert launcher.main(["--unrecognized", "synthetic-sensitive-value-never-echo"]) == 1
    captured = capsys.readouterr()
    assert "synthetic-sensitive-value-never-echo" not in captured.out + captured.err


def test_explicit_future_opt_in_reaches_only_the_stubbed_diagnostic_server(monkeypatch, tmp_path):
    fake_environment = {"LANGSMITH_API_KEY": "synthetic-never-used", "CLINICAL_TRACE_LLM_PROVIDER": "synthetic"}
    monkeypatch.setattr(launcher.os, "environ", fake_environment)
    calls = []
    monkeypatch.setattr(launcher, "_serve", lambda certificate, key: calls.append((certificate, key)))
    certificate, key = tmp_path / "invented.pem", tmp_path / "invented.key"
    assert launcher.main(opt_in(certificate, key)) == 0
    assert calls == [(certificate, key)]
    assert fake_environment["CLINICAL_TRACE_SKIP_DOTENV"] == "1"
    assert fake_environment["CLINICAL_TRACE_LLM_PROVIDER"] == "none"
    assert fake_environment["OTEL_SDK_DISABLED"] == "true"
    assert "LANGSMITH_API_KEY" not in fake_environment


@pytest.mark.parametrize("failure", [RuntimeError("synthetic-private-path-and-passphrase"), SystemExit(7)])
def test_preparation_or_serve_failure_is_content_free_and_never_retries(monkeypatch, tmp_path, capsys, failure):
    monkeypatch.setattr(launcher.os, "environ", {})
    calls = []

    def refuse(*_args):
        calls.append("attempt")
        raise failure

    monkeypatch.setattr(launcher, "_serve", refuse)
    certificate = tmp_path / "synthetic-private-path-and-passphrase.pem"
    assert launcher.main(opt_in(certificate, tmp_path / "invented.key")) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "synthetic-private-path-and-passphrase" not in output
    assert "Traceback" not in output and calls == ["attempt"]


def test_interrupt_stops_without_restart_or_error_details(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.os, "environ", {})
    calls = []

    def interrupted(*_args):
        calls.append("attempt")
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "_serve", interrupted)
    assert launcher.main(opt_in(tmp_path / "invented.pem", tmp_path / "invented.key")) == 0
    assert calls == ["attempt"]


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"])
def test_ambient_trust_or_key_logging_override_refuses_before_serve(monkeypatch, tmp_path, capsys, variable):
    monkeypatch.setattr(launcher.os, "environ", {variable: "synthetic-private-override-value"})
    monkeypatch.setattr(launcher, "_serve", lambda *_: pytest.fail("ambient override reached certificate/serve path"))
    assert launcher.main(opt_in(tmp_path / "invented.pem", tmp_path / "invented.key")) == 1
    captured = capsys.readouterr()
    assert "synthetic-private-override-value" not in captured.out + captured.err


def test_preparer_pins_dedicated_policy_and_only_the_diagnostic_bootstrap(monkeypatch, tmp_path):
    sentinel = object()
    calls = []

    def bootstrap(policy, certificate, key, *, enabled=False):
        calls.append((policy, certificate, key, enabled))
        return sentinel

    monkeypatch.setattr(https, "prepare_bootstrap", bootstrap)
    certificate, key = tmp_path / "invented.pem", tmp_path / "invented.key"
    assert launcher._prepare(certificate, key) is sentinel
    assert len(calls) == 1
    policy, supplied_certificate, supplied_key, enabled = calls[0]
    assert isinstance(policy, LocalOwnerPolicy)
    assert (policy.origin, policy.rp_id, policy.authority, policy.purpose) == (
        OWNER_ORIGIN,
        OWNER_RP_ID,
        "owner.clinical-trace.localhost:8443",
        "offline-synthetic-v1",
    )
    assert (supplied_certificate, supplied_key, enabled) == (certificate, key, True)


def test_server_adapter_runs_only_the_prepared_configuration_with_stubbed_run(monkeypatch, tmp_path):
    sentinel, calls = object(), []
    monkeypatch.setattr(launcher, "_prepare", lambda *_: sentinel)

    class StubServer:
        def __init__(self, configuration):
            assert configuration is sentinel
            calls.append("construct")

        def run(self):
            calls.append("run-stub-only")

    monkeypatch.setattr(uvicorn, "Server", StubServer)
    launcher._serve(tmp_path / "invented.pem", tmp_path / "invented.key")
    assert calls == ["construct", "run-stub-only"]


def test_shutdown_bounds_an_unclosed_tls_connection_without_a_listener(monkeypatch, tmp_path):
    import ssl
    from types import SimpleNamespace

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    monkeypatch.setattr(https, "_trusted_server", lambda *args, **kwargs: context)
    config = https.prepare_bootstrap(
        LocalOwnerPolicy(OWNER_ORIGIN, HANDLE), tmp_path / "never-read.pem", tmp_path / "never-read.key", enabled=True
    )
    assert config.timeout_graceful_shutdown == 5
    config.timeout_graceful_shutdown = 0.01  # exercise the configured timeout without a five-second test wait

    class UnclosedServer:
        closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            await asyncio.Future()

    async def check():
        endpoint = UnclosedServer()
        server = uvicorn.Server(config)
        server.servers = [endpoint]
        server.force_exit = True
        server.lifespan = SimpleNamespace()
        await asyncio.wait_for(server.shutdown(), timeout=1)
        assert endpoint.closed

    asyncio.run(check(), loop_factory=_MemoryLoop)


def test_import_surface_cannot_activate_normal_apps_auth_browser_or_external_clients():
    source = Path(launcher.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_imports = [
        node.module or "" if isinstance(node, ast.ImportFrom) else alias.name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    ]
    assert not any(name.startswith(("api", "uvicorn", "fastapi", "fido2", "cryptography")) for name in top_imports)
    imports = [
        node.module or "" if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    ]
    prohibited = (
        "api.main",
        "api.beta_local",
        "api.beta_browser",
        "api.beta_webauthn_http",
        "webbrowser",
        "subprocess",
        "requests",
        "httpx",
        "socket",
    )
    assert not any(name.startswith(prohibited) for name in imports)
    assert not any(isinstance(node, ast.Name) and node.id == "LocalOwnerAuth" for node in ast.walk(tree))


def test_diagnostic_configuration_overrides_ambient_trace_before_loading_without_scope_logs(
    dedicated_certificates, monkeypatch, caplog
):
    certificate, key, _ = dedicated_certificates([OWNER_RP_ID])

    class SyntheticCustody:
        def __init__(self, _root):
            pass

        def __enter__(self):
            return self

        def file(self, _path):
            pass

        def check(self):
            pass

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(https, "Custody", SyntheticCustody)
    loggers = [logging.getLogger(name) for name in ("uvicorn.error", "uvicorn.asgi", "uvicorn.access")]
    prior = [(logger, logger.level, list(logger.handlers), logger.propagate) for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(5)  # inherited uvicorn TRACE must not wrap the diagnostic in message logging
        caplog.set_level(5)
        config = https.prepare_bootstrap(LocalOwnerPolicy(OWNER_ORIGIN, HANDLE), certificate, key, enabled=True)
        assert config.log_level == "warning" and not config.access_log
        assert not isinstance(config.loaded_app, MessageLoggerMiddleware)
        assert all(logger.getEffectiveLevel() >= logging.WARNING for logger in loggers)
        start, body = diagnostic_response(
            config.loaded_app,
            headers=[(b"host", b"owner.clinical-trace.localhost:8443")],
            query_string=b"synthetic_scope_value_never_log=1",
        )
        assert start["status"] == 403 and b"synthetic_scope_value_never_log" not in body
        assert "synthetic_scope_value_never_log" not in caplog.text
        assert "HTTP connection" not in caplog.text and "ASGI" not in caplog.text
    finally:
        for logger, level, handlers, propagate in prior:
            logger.setLevel(level)
            logger.handlers = handlers
            logger.propagate = propagate
