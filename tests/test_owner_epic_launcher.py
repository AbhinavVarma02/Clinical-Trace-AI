"""Launcher phase and command validation with no listener, credentials, or private storage."""

from pathlib import Path

import pytest

from scripts import run_owner_epic as launch


def _base(phase):
    root = Path.cwd().anchor
    return [
        "--phase",
        phase,
        "--accept-owner-policy",
        "--binding-root",
        str(Path(root) / "test-binding"),
        "--binding-sha256",
        "d" * 64,
        "--certificate",
        str(Path(root) / "test-certificate.pem"),
        "--private-key",
        str(Path(root) / "test-tls.key"),
        "--assets",
        str(Path(root) / "test-assets"),
        "--private-root",
        str(Path(root) / "test-epic-private"),
        "--alias",
        "invented-alpha",
        "--patient-id",
        "invented-live-patient",
    ]


def _acquisition():
    return [
        "--approval-id",
        "n" * 43,
        "--client-id",
        "invented-client",
        "--epic-key",
        str(Path(Path.cwd().anchor) / "test-epic.key"),
        "--epic-key-id",
        "k" * 43,
    ]


_PINS = [
    "--attempt-id",
    "a" * 32,
    "--generation-id",
    "b" * 32,
    "--manifest-sha256",
    "c" * 64,
    "--bundle-sha256",
    "d" * 64,
]


@pytest.mark.parametrize("phase", ["acquire", "review", "promote", "serve"])
def test_one_complete_phase_reaches_only_the_operator_boundary(phase, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "_serve", calls.append)
    extras = []
    if phase == "acquire":
        extras = _acquisition()
    elif phase != "serve":
        extras = ["--approval-id", "n" * 43]
    if phase in {"promote", "serve"}:
        extras += _PINS
    assert launch.main(_base(phase) + extras) == 0
    assert len(calls) == 1 and calls[0].phase == phase


@pytest.mark.parametrize(
    "phase,extras",
    [
        ("acquire", []),
        ("review", _acquisition()),
        ("promote", _acquisition() + _PINS),
        ("serve", _acquisition() + _PINS),
        ("serve", _PINS[:-2]),
    ],
)
def test_no_phase_can_import_runtime_with_missing_or_other_phase_arguments(phase, extras, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "_serve", calls.append)
    assert launch.main([]) == 2
    assert launch.main(_base(phase) + extras) == 1
    assert calls == []
