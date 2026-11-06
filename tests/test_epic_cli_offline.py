"""The Epic CLI stays offline: no repository .env, no LangSmith tracing, no language model (MOCK/offline).

The repository ``.env`` may enable LangSmith tracing, and ``src.config`` loads it when first imported.
An agent run traced to LangSmith would upload Epic test-patient content to a third party. These tests
run the real CLI in a fresh interpreter with outbound sockets blocked, with tracing switched on both by
a simulated ``.env`` and by the inherited shell environment, and check that nothing leaves the machine.
They also check that ``workspace review`` refuses to run when offline execution cannot be guaranteed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.interop.epic import cli, settings, workspace
from tests.epic_support import RETRIEVED_AT, fixture_patient

REPO_ROOT = Path(__file__).resolve().parents[1]
QUESTION = "Summarize this patient's hospital course"
FAKE_KEY = "lsv2_pt_" + "0" * 32 + "_not_a_real_key"

# Runs in a fresh interpreter so the CLI's import-time guard is exercised exactly as `python -m` would.
_CHILD = r'''
import contextlib, io, json, os, socket, sys
from pathlib import Path

repo, workspace_root, fake_env, preload = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "preload"
LOOPBACK = {"127.0.0.1", "::1", "localhost", None}
attempts = []
_connect, _connect_ex, _getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

def _host(address):
    return address[0] if isinstance(address, tuple) else address

def connect(self, address):
    if _host(address) not in LOOPBACK:
        attempts.append(str(_host(address)))
        raise OSError("outbound network blocked by test")
    return _connect(self, address)

def connect_ex(self, address):
    if _host(address) not in LOOPBACK:
        attempts.append(str(_host(address)))
        raise OSError("outbound network blocked by test")
    return _connect_ex(self, address)

def getaddrinfo(host, *args, **kwargs):
    if host not in LOOPBACK:
        attempts.append(str(host))
        raise OSError("outbound network blocked by test")
    return _getaddrinfo(host, *args, **kwargs)

socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo

# Simulate a repository .env that enables LangSmith tracing, whatever file src.config asks for.
import dotenv
dotenv_calls = []
_load = dotenv.load_dotenv
def load_dotenv(*args, **kwargs):
    dotenv_calls.append(1)
    return _load(fake_env, override=True)
dotenv.load_dotenv = load_dotenv

sys.path.insert(0, repo)
os.chdir(repo)
if preload:
    import src.config  # another module loaded .env (tracing on) before the CLI was imported
from src.interop.epic import cli, settings, workspace
from tests.epic_support import RETRIEVED_AT, fixture_patient

settings.REPO_ROOT = Path(workspace_root)
pid, resources, binaries = fixture_patient()
workspace.write_snapshot(pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK",
                         snapshot_id="offline-guard-01")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = cli.main(["workspace", "review", "offline-guard-01", "--question", "%s"])
from langchain_core.tracers.langchain import wait_for_all_tracers
wait_for_all_tracers()  # flush any queued trace uploads so attempts are counted
from langsmith import utils as ls_utils
payload = json.loads(out.getvalue())
print("RESULT:" + json.dumps({
    "exit": code,
    "outcome": payload.get("outcome"),
    "inference": payload.get("inference"),
    "attempts": attempts,
    "dotenv_calls": len(dotenv_calls),
    "tracing_enabled": bool(ls_utils.tracing_is_enabled()),
    "api_key_in_env": any(os.environ.get(k) for k in ("LANGCHAIN_API_KEY", "LANGSMITH_API_KEY")),
}))
''' % QUESTION


@pytest.mark.parametrize("preload", ["fresh", "preload"])
def test_review_sends_no_tracing_requests_even_when_env_and_shell_enable_langsmith(tmp_path, preload):
    fake_env = tmp_path / "fake.env"
    fake_env.write_text(f"LANGCHAIN_TRACING_V2=true\nLANGCHAIN_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()

    env = dict(os.environ)
    for name in ("CLINICAL_TRACE_SKIP_DOTENV", "CLINICAL_TRACE_DB_PATH", settings.ENABLE_ENV):
        env.pop(name, None)
    # Tracing switched on by the inherited shell as well as by the simulated .env.
    env.update({"LANGCHAIN_TRACING_V2": "true", "LANGSMITH_TRACING": "true", "LANGCHAIN_API_KEY": FAKE_KEY})

    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(REPO_ROOT), str(workspace_root), str(fake_env), preload],
        env=env, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stderr[-2000:]
    result = json.loads(lines[-1][len("RESULT:"):])

    assert result["attempts"] == [], result  # nothing left the machine
    assert result["tracing_enabled"] is False
    assert result["api_key_in_env"] is False
    assert result["exit"] == 0 and result["outcome"] == "answered"
    assert "no language model was called" in result["inference"]
    if preload == "fresh":
        assert result["dotenv_calls"] == 0  # the CLI never loads the repository .env itself


@pytest.fixture
def mock_snapshot(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(settings, "REPO_ROOT", repo)
    monkeypatch.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, binaries = fixture_patient()
    workspace.write_snapshot(pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK",
                             snapshot_id="offline-guard-02")
    return "offline-guard-02"


def test_review_refuses_before_building_the_runtime_when_tracing_is_enabled(mock_snapshot, monkeypatch, capsys):
    from langsmith import utils as ls_utils

    monkeypatch.setattr(ls_utils, "tracing_is_enabled", lambda *args, **kwargs: True)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("the runtime must not be built when offline execution is not guaranteed")

    monkeypatch.setattr(workspace, "load_runtime", must_not_run)
    code = cli.main(["workspace", "review", mock_snapshot, "--question", QUESTION])
    output = json.loads(capsys.readouterr().out)
    assert code == 2 and output["ok"] is False
    assert "tracing" in output["error"]


def test_review_refuses_when_a_language_model_provider_is_configured(mock_snapshot, monkeypatch, capsys):
    closed = []

    class _Agent:
        def run(self, *_args, **_kwargs):
            raise AssertionError("the agent must not run when a provider is configured")

    class _Runtime:
        provider = object()
        agent = _Agent()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(workspace, "load_runtime", lambda *_args, **_kwargs: _Runtime())
    code = cli.main(["workspace", "review", mock_snapshot, "--question", QUESTION])
    output = json.loads(capsys.readouterr().out)
    assert code == 2 and output["ok"] is False
    assert "provider" in output["error"]
    assert closed == [True]
