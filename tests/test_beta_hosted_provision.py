"""Offline contract for the owner-run hosted secret provisioning. Invented config, JWK Set and runner; no upload."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api.beta_hosted.hosted_keys import load_pem_key
from scripts import provision_beta_live_secrets as provision
from src.interop.epic import keys, settings

EXISTING = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JKU = "https://keys.invalid/jwks.json"
ANSWERS = ["https://invented-team.cloudflareaccess.com", "a" * 64, "owner@invented.example", "invented-live-alpha"]


@pytest.fixture
def setup(monkeypatch, tmp_path):
    config = settings.EpicConfig("invented-client-0001", tmp_path / "never-read.key", JKU)
    monkeypatch.setattr(settings, "load_config", lambda: config)
    published = keys.build_jwks([keys.public_jwk(EXISTING)])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=published))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    answers = iter(ANSWERS)
    return {"transport": transport, "run": run, "calls": calls, "reader": lambda _: next(answers), "out": tmp_path / "jwks.json"}


def test_secrets_go_only_to_wrangler_stdin_and_the_jwks_keeps_the_existing_key(setup, capsys):
    code = provision.main(["--jwks-out", str(setup["out"])], reader=setup["reader"], run=setup["run"], transport=setup["transport"])
    assert code == 0
    [(args, kwargs)] = setup["calls"]
    assert args[1:] == ["wrangler", "secret", "bulk", "--config", "beta-live/wrangler.jsonc"]
    secrets = json.loads(kwargs["input"])
    assert set(secrets) == set(provision.SECRET_NAMES)
    assert secrets["EPIC_CLIENT_ID"] == "invented-client-0001" and secrets["EPIC_JKU"] == JKU
    hosted = load_pem_key(secrets["EPIC_PRIVATE_KEY_PEM"].encode(), secrets["EPIC_KEY_ID"])
    jwks = json.loads(setup["out"].read_text(encoding="utf-8"))
    assert [jwk["kid"] for jwk in jwks["keys"]] == [keys.key_id(EXISTING), keys.key_id(hosted)]
    assert not keys.lint_jwks(jwks) and "PRIVATE" not in setup["out"].read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert secrets["EPIC_KEY_ID"] in printed
    for value in (secrets["EPIC_PRIVATE_KEY_PEM"][40:80], "invented-client-0001", "owner@invented.example", "a" * 64):
        assert value not in printed


def test_dry_run_and_failed_upload_write_nothing(setup):
    assert provision.main(["--dry-run", "--jwks-out", str(setup["out"])], reader=setup["reader"], run=setup["run"],
                          transport=setup["transport"]) == 0
    assert setup["calls"] == [] and not setup["out"].exists()

    def failing(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "")

    answers = iter(ANSWERS)
    with pytest.raises(SystemExit):
        provision.main(["--jwks-out", str(setup["out"])], reader=lambda _: next(answers), run=failing, transport=setup["transport"])
    assert not setup["out"].exists()


@pytest.mark.parametrize("index,value", [(0, "https://evil.example.com"), (1, "short"), (2, "not-an-email"), (3, "../x")])
def test_bad_inputs_stop_before_any_key_or_upload(setup, index, value):
    answers = list(ANSWERS)
    answers[index] = value
    feed = iter(answers)
    with pytest.raises((ValueError, SystemExit)):
        provision.main(["--jwks-out", str(setup["out"])], reader=lambda _: next(feed), run=setup["run"], transport=setup["transport"])
    assert setup["calls"] == [] and not setup["out"].exists()


def test_the_jwks_file_must_be_outside_the_repository(setup):
    inside = Path(provision.ROOT) / "jwks.json"
    with pytest.raises(settings.LocalPathError):
        provision.main(["--jwks-out", str(inside)], reader=setup["reader"], run=setup["run"], transport=setup["transport"])
    assert setup["calls"] == [] and not inside.exists()
