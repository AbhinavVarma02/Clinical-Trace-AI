"""Invented console boundaries only; no real console, SID or credential operation."""

import pytest

from api import beta_owner_console as console

PRINCIPAL = "invented-owner"
FINGERPRINT = "a" * 64


def boundaries(monkeypatch, *, response=None, elapsed=1.0, console_after=True, principal_after=PRINCIPAL):
    calls = []
    console_values = iter([True, console_after])
    principals = iter([PRINCIPAL, principal_after])
    moments = iter([100.0, 100.0 + elapsed])

    def local():
        calls.append("console")
        value = next(console_values)
        if isinstance(value, Exception):
            raise value
        return value

    def principal():
        calls.append("principal")
        value = next(principals)
        if isinstance(value, Exception):
            raise value
        return value

    def read(prompt):
        calls.append("input")
        if isinstance(response, Exception):
            raise response
        return "approve " + FINGERPRINT if response is None else response

    def now():
        calls.append("clock")
        return next(moments)

    monkeypatch.setattr(console, "_local_console", local)
    monkeypatch.setattr(console, "current_principal", principal)
    monkeypatch.setattr("builtins.input", read)
    monkeypatch.setattr(console.time, "monotonic", now)
    return calls


@pytest.mark.parametrize("elapsed,accepted", [(59.999, True), (60.0, False), (61.0, False)])
def test_original_prompt_deadline_and_guard_order(monkeypatch, elapsed, accepted):
    calls = boundaries(monkeypatch, elapsed=elapsed)
    observed = []
    assert (
        console.confirm_owner_action(
            PRINCIPAL, FINGERPRINT, enabled=True, diagnostic=lambda *value: observed.append(value)
        )
        is accepted
    )
    expected = ["console", "principal", "clock", "input", "clock"]
    if accepted:
        expected += ["console", "principal"]
    assert calls == expected
    assert len(observed) == 1
    assert observed[0][0] == ("approved" if accepted else "prompt_expired")
    assert abs(observed[0][1] - int(elapsed * 1000)) <= 1


@pytest.mark.parametrize(
    "response",
    [
        "deny",
        "approve " + FINGERPRINT + " ",
        "$CtRetryExit = $LASTEXITCODE",
        "approve " + FINGERPRINT + "\n$CtRetryExit = $LASTEXITCODE",
    ],
)
def test_pasted_or_mismatched_input_never_reaches_postinput_authority_checks(monkeypatch, response):
    calls = boundaries(monkeypatch, response=response, elapsed=12.0)
    observed = []
    assert not console.confirm_owner_action(
        PRINCIPAL, FINGERPRINT, enabled=True, diagnostic=lambda *value: observed.append(value)
    )
    assert calls == ["console", "principal", "clock", "input", "clock"]
    assert observed == [("response_mismatch", 12000)]


@pytest.mark.parametrize(
    "changes,outcome,expected_calls",
    [
        ({"console_after": False}, "console_recheck_refused", ["console"]),
        ({"principal_after": "another-invented-owner"}, "principal_recheck_refused", ["console", "principal"]),
        ({"console_after": RuntimeError("invented detail")}, "console_recheck_refused", ["console"]),
        (
            {"principal_after": RuntimeError("invented detail")},
            "principal_recheck_refused",
            ["console", "principal"],
        ),
    ],
)
def test_postinput_refusals_have_fixed_labels(monkeypatch, changes, outcome, expected_calls):
    calls = boundaries(monkeypatch, **changes)
    observed = []
    assert not console.confirm_owner_action(
        PRINCIPAL, FINGERPRINT, enabled=True, diagnostic=lambda *value: observed.append(value)
    )
    assert calls == ["console", "principal", "clock", "input", "clock", *expected_calls]
    assert observed == [(outcome, 1000)]


def test_input_exception_has_no_secret_or_raw_exception_diagnostic(monkeypatch):
    calls = boundaries(monkeypatch, response=RuntimeError("invented secret detail"))
    observed = []
    assert not console.confirm_owner_action(
        PRINCIPAL, FINGERPRINT, enabled=True, diagnostic=lambda *value: observed.append(value)
    )
    assert calls == ["console", "principal", "clock", "input"]
    assert observed == [("input_failed", 0)]


@pytest.mark.parametrize("accepted", [False, True])
def test_observer_failure_cannot_change_authorization(monkeypatch, accepted):
    boundaries(monkeypatch, response=None if accepted else "deny")

    def observer(*_):
        raise RuntimeError("invented observer failure")

    assert console.confirm_owner_action(PRINCIPAL, FINGERPRINT, enabled=True, diagnostic=observer) is accepted


def test_default_output_and_mismatch_short_circuit_are_unchanged(monkeypatch, capsys):
    calls = boundaries(monkeypatch, response="deny")
    assert not console.confirm_owner_action(PRINCIPAL, FINGERPRINT, enabled=True)
    assert calls == ["console", "principal", "clock", "input"]
    assert capsys.readouterr().out == (
        "Clinical-Trace owner action at https://owner.clinical-trace.localhost:8443\n"
        f"Compare this ENTIRE fingerprint with the intended browser/configuration: {FINGERPRINT}\n"
    )


def test_disabled_diagnostics_do_not_cross_any_boundary(monkeypatch):
    def forbidden(*_):
        pytest.fail("disabled call crossed a boundary")

    monkeypatch.setattr(console, "_local_console", forbidden)
    monkeypatch.setattr(console, "current_principal", forbidden)
    monkeypatch.setattr(console.time, "monotonic", forbidden)
    assert not console.confirm_owner_action(PRINCIPAL, FINGERPRINT, diagnostic=forbidden)
