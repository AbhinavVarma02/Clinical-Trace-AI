import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import provision_owner_genuine as launcher

SID = "S-1-5-21-11-22-33-1001"
requires_windows = pytest.mark.skipif(
    sys.platform != "win32" or sys.version_info < (3, 13),
    reason="Genuine NTFS provisioning requires Windows Python 3.13",
)


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ([], 2),
        (["--diagnostic-timing"], 2),
        (["--provision-owner-binding"], 1),
        (["--provision-owner-binding", "--accept-owner-policy", "--binding-root", "relative"], 1),
        (["--provision-owner"], 1),
    ],
)
def test_refuses_unapproved_invocations_before_native_work(monkeypatch, extra, status):
    operation = Mock()
    monkeypatch.setattr(launcher, "_provision", operation)
    assert launcher.main(extra) == status
    operation.assert_not_called()


@requires_windows
def test_only_provisions_once_and_returns_binding_digest(monkeypatch, capsys, tmp_path):
    operation = Mock(return_value="a" * 64)
    monkeypatch.setattr(launcher, "_provision", operation)
    root = tmp_path / "new"
    args = [
        "--provision-owner-binding",
        "--accept-owner-policy",
        "--binding-root",
        str(root),
        "--approved-sid",
        SID,
        "--create-private-parent",
    ]
    assert launcher.main(args) == 0
    operation.assert_called_once_with(root, SID, create_private_parent=True)
    assert capsys.readouterr().out == f"Owner binding SHA256: {'a' * 64}\n"


@pytest.mark.parametrize("save_fails", [False, True])
@requires_windows
def test_attended_creation_checks_custody_before_store_and_closes_on_binding_failure(monkeypatch, tmp_path, save_fails):
    from api import beta_owner_console, beta_owner_custody, beta_owner_genuine, beta_owner_operator, beta_webauthn_store
    from scripts import run_owner_https_diagnostic

    calls = []
    root = tmp_path / "private-parent" / "new-store"
    store = Mock()
    monkeypatch.setattr(run_owner_https_diagnostic, "_lock_environment", lambda: calls.append("locked"))
    monkeypatch.setattr(beta_webauthn_store, "current_principal", lambda: SID)
    monkeypatch.setattr(beta_owner_console, "confirm_owner_action", lambda *a, **k: calls.append("confirmed") or True)

    class CheckedCustody:
        def __init__(self, directory: Path, *, private_leaf=True) -> None:
            self.private_leaf = private_leaf
            assert directory == (root.parent if private_leaf else tmp_path)
            assert directory.is_dir() and root.parent.exists() is private_leaf

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def check(self):
            calls.append("private" if self.private_leaf else "ancestor")

    def provision(path, principal, *, enabled, policy_accepted):
        assert path == root and principal == SID and enabled and policy_accepted
        calls.append("provision")
        return store

    def save(binding_store, *, enabled):
        assert binding_store is store and enabled
        calls.append("bind")
        if save_fails:
            raise ValueError("synthetic binding write failed")
        return "b" * 64

    monkeypatch.setattr(beta_owner_custody, "Custody", CheckedCustody)
    monkeypatch.setattr(beta_owner_genuine, "provision_owner_store", provision)
    monkeypatch.setattr(beta_owner_operator, "save_owner_binding", save)
    if save_fails:
        with pytest.raises(ValueError, match="synthetic binding write failed"):
            launcher._provision(root, SID, create_private_parent=True)
    else:
        assert launcher._provision(root, SID, create_private_parent=True) == "b" * 64
    assert calls == ["locked", "confirmed", "ancestor", "private", "provision", "bind"]
    store.close.assert_called_once()


@requires_windows
def test_unsafe_ancestor_refuses_before_creating_any_directory(monkeypatch, tmp_path):
    from api import beta_owner_console, beta_owner_custody, beta_owner_genuine, beta_webauthn_store
    from scripts import run_owner_https_diagnostic

    root = tmp_path / "uncreated-parent" / "uncreated-store"
    provision = Mock()
    monkeypatch.setattr(run_owner_https_diagnostic, "_lock_environment", Mock())
    monkeypatch.setattr(beta_webauthn_store, "current_principal", lambda: SID)
    monkeypatch.setattr(beta_owner_console, "confirm_owner_action", lambda *a, **k: True)
    monkeypatch.setattr(beta_owner_custody, "Custody", Mock(side_effect=beta_owner_custody.CustodyError))
    monkeypatch.setattr(beta_owner_genuine, "provision_owner_store", provision)
    with pytest.raises(beta_owner_custody.CustodyError):
        launcher._provision(root, SID, create_private_parent=True)
    assert not root.parent.exists()
    provision.assert_not_called()


def test_ancestor_only_custody_cannot_pin_private_files(tmp_path):
    from api.beta_owner_custody import Custody, CustodyError

    custody = Custody.__new__(Custody)
    custody._private_leaf = False
    with pytest.raises(CustodyError):
        custody.file(tmp_path / "never-opened", create=True)


@requires_windows
def test_diagnostic_flag_is_explicit_and_does_not_change_provisioning_scope(monkeypatch, tmp_path):
    operation = Mock(return_value="a" * 64)
    monkeypatch.setattr(launcher, "_provision", operation)
    root = tmp_path / "new"
    assert (
        launcher.main(
            [
                "--provision-owner-binding",
                "--accept-owner-policy",
                "--binding-root",
                str(root),
                "--approved-sid",
                SID,
                "--create-private-parent",
                "--diagnostic-timing",
            ]
        )
        == 0
    )
    operation.assert_called_once_with(root, SID, create_private_parent=True, diagnostic_timing=True)


@requires_windows
@pytest.mark.parametrize(
    "failure,last_started,not_reached",
    [
        ("confirmation", "parent_confirmation_begin", "ancestor_custody_begin"),
        ("ancestor_custody", "ancestor_custody_begin", "ancestor_check"),
        ("ancestor_check", "ancestor_check", "parent_create"),
        ("parent_create", "parent_create", "parent_custody_begin"),
        ("parent_custody", "parent_custody_begin", "parent_check"),
        ("parent_check", "parent_check", "store_provision_begin"),
        ("store", "store_provision_begin", "store_provision_ready"),
        ("binding", "binding_write_begin", "binding_write_ready"),
        ("close", "store_close_begin", "store_close_ready"),
    ],
)
def test_diagnostics_localize_invented_failures_without_later_work(
    monkeypatch, tmp_path, capsys, failure, last_started, not_reached
):
    from api import beta_owner_console, beta_owner_custody, beta_owner_genuine, beta_owner_operator, beta_webauthn_store
    from scripts import run_owner_https_diagnostic

    root = tmp_path / "invented-parent" / "invented-store"
    operations = []
    store = Mock()
    moments = iter(range(1000))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(run_owner_https_diagnostic, "_lock_environment", lambda: None)
    monkeypatch.setattr(beta_webauthn_store, "current_principal", lambda: SID)

    def fail_if(stage):
        operations.append(stage)
        if failure == stage:
            raise RuntimeError("invented detail that must not enter diagnostics")

    def confirm(principal, fingerprint, *, enabled, diagnostic):
        assert principal == SID and enabled
        assert len(fingerprint) == 64
        diagnostic("response_mismatch" if failure == "confirmation" else "approved", 1250)
        operations.append("confirmation")
        return failure != "confirmation"

    class CheckedCustody:
        def __init__(self, directory, *, private_leaf=True):
            self.stage = "parent" if private_leaf else "ancestor"
            assert directory == (root.parent if private_leaf else tmp_path)
            fail_if(self.stage + "_custody")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def check(self):
            fail_if(self.stage + "_check")

    real_mkdir = Path.mkdir

    def mkdir(path, *args, **kwargs):
        assert path == root.parent
        fail_if("parent_create")
        return real_mkdir(path, *args, **kwargs)

    def provision(path, principal, *, enabled, policy_accepted):
        assert path == root and principal == SID and enabled and policy_accepted
        fail_if("store")
        return store

    def bind(binding_store, *, enabled):
        assert binding_store is store and enabled
        fail_if("binding")
        return "b" * 64

    store.close.side_effect = lambda: fail_if("close")
    monkeypatch.setattr(beta_owner_console, "confirm_owner_action", confirm)
    monkeypatch.setattr(beta_owner_custody, "Custody", CheckedCustody)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(beta_owner_genuine, "provision_owner_store", provision)
    monkeypatch.setattr(beta_owner_operator, "save_owner_binding", bind)
    with pytest.raises((RuntimeError, launcher.ProvisionRejected)):
        launcher._provision(root, SID, create_private_parent=True, diagnostic_timing=True)
    diagnostics = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Owner diagnostic ")]
    stages = [line.split("stage=", 1)[1].split(" ", 1)[0] for line in diagnostics]
    assert last_started in stages and not_reached not in stages
    assert stages.index("dependencies_begin") < stages.index("dependencies_ready")
    assert stages.index("dependencies_ready") < stages.index("parent_confirmation_begin")
    assert all(line.rsplit("elapsed_ms=", 1)[1].isdigit() for line in diagnostics)
    assert all("invented detail" not in line and SID not in line and str(root) not in line for line in diagnostics)
    if failure in {"binding", "close"}:
        store.close.assert_called_once()
    else:
        store.close.assert_not_called()
