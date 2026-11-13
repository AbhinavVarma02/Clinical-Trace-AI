"""Offline custody attacks against synthetic SQLite and MemoryBIO TLS assets only."""

from __future__ import annotations

import os
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from api import beta_owner_custody as custody
from api import beta_owner_https as https
from api import beta_webauthn_store as storage
from api.beta_webauthn import LocalOwnerPolicy
from tests.test_beta_webauthn import HANDLE, ORIGIN, enroll
from tests.test_beta_webauthn_store import PRINCIPAL

pytest_plugins = ["tests.test_beta_owner_https", "tests.test_beta_webauthn_store"]

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Windows/NTFS handle custody")


@windows_only
@pytest.mark.parametrize("rights,allowed", [("F", False), ("WA", False), ("RX", True)])
def test_ancestor_permissions_reject_mutation_but_allow_public_read(closed_owner, rights, allowed):
    root, policy, expected, ancestor = closed_owner
    result = subprocess.run(
        ["icacls", str(ancestor), "/grant", f"*S-1-1-0:({rights})"], capture_output=True, check=False
    )
    assert result.returncode == 0, "synthetic ancestor permission setup failed"
    try:
        if allowed:
            store = storage.OwnerCredentialStore(root, policy, PRINCIPAL)
            try:
                assert store.load() == expected
            finally:
                store.close()
        else:
            with pytest.raises(storage.StoreUnavailable):
                storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    finally:
        restored = subprocess.run(["icacls", str(ancestor), "/remove:g", "*S-1-1-0"], capture_output=True, check=False)
        assert restored.returncode == 0, "synthetic ancestor permission cleanup failed"


@windows_only
def test_failed_sqlite_close_retains_custody_until_successful_close(closed_owner):
    root, policy, _, _ = closed_owner
    store = storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    connection = store._connection

    class FailedClose:
        def close(self):
            raise OSError("synthetic SQLite closure failure")

    store._connection = FailedClose()
    try:
        store.close()
        assert store._failed
        assert_rename_denied(root)
    finally:
        store._connection = connection
        store.close()
    # Successful close releases custody, allowing legitimate offline owner maintenance.
    displaced = root.with_name("closed-owner")
    root.rename(displaced)
    displaced.rename(root)


@pytest.fixture
def closed_owner(durable, tmp_path):
    owner, store, signer = durable
    enroll(owner, signer)
    expected = store.load()
    store.close()
    # Every rename target, replacement name and junction target belongs to this test.
    ancestor = tmp_path / "synthetic-owner-ancestor"
    parent = ancestor / "synthetic-owner-parent"
    parent.mkdir(parents=True, mode=0o700)
    os.chmod(ancestor, 0o700)
    root = parent / "owner"
    store.root.rename(root)
    return root, store.policy, expected, ancestor


@pytest.fixture
def private_tls(certificates, tmp_path):
    certificate, key, client = certificates()
    ancestor = tmp_path / "synthetic-tls-ancestor"
    parent = ancestor / "synthetic-tls-parent"
    parent.mkdir(parents=True, mode=0o700)
    os.chmod(ancestor, 0o700)
    relocated_certificate, relocated_key = parent / certificate.name, parent / key.name
    certificate.rename(relocated_certificate)
    key.rename(relocated_key)
    leaf = x509.load_pem_x509_certificate(relocated_certificate.read_bytes()).public_bytes(Encoding.DER)
    return relocated_certificate, relocated_key, client, leaf, ancestor


def assert_rename_denied(path: Path) -> None:
    """Try the attack; restore the synthetic asset before reporting a protection failure."""
    displaced = path.with_name(path.name + ".synthetic-displaced")
    assert not os.path.lexists(displaced)
    try:
        path.rename(displaced)
    except PermissionError:
        return
    else:
        displaced.rename(path)
        pytest.fail("a pinned synthetic filesystem component could be renamed")


def make_hardlink(source: Path, link: Path) -> None:
    try:
        os.link(source, link)
    except (NotImplementedError, PermissionError):
        pytest.skip("this sandbox does not permit creating a real synthetic hardlink")
    assert source.stat().st_nlink == link.stat().st_nlink == 2


@pytest.fixture
def junction(tmp_path):
    created = []

    def make(target: Path) -> Path:
        assert target.is_relative_to(tmp_path)
        alias = tmp_path / "synthetic-junction"
        assert not os.path.lexists(alias)
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        # cmd.exe creates only this junction; cleanup stays in Python and never traverses it.
        if os.path.lexists(alias):
            created.append(alias)
        assert result.returncode == 0, "the synthetic Windows junction could not be created"
        assert alias.lstat().st_file_attributes & 0x400
        return alias

    yield make
    for alias in reversed(created):
        os.rmdir(alias)


@windows_only
@pytest.mark.parametrize("component", ["database", "root", "parent", "ancestor"])
def test_sqlite_reopen_cannot_replace_a_pinned_component_before_connect(closed_owner, monkeypatch, component):
    root, policy, expected, ancestor = closed_owner
    target = {"database": root / "owner.sqlite3", "root": root, "parent": root.parent, "ancestor": ancestor}[component]
    real_connect = storage.sqlite3.connect
    attempts = []

    def connect(*args, **kwargs):
        assert_rename_denied(target)
        attempts.append(target)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(storage.sqlite3, "connect", connect)
    store = storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    try:
        assert attempts == [target]
        assert store.load() == expected
        assert store.load().credential is not None
    finally:
        store.close()


@windows_only
@pytest.mark.parametrize("component", ["database", "root", "parent", "ancestor"])
def test_sqlite_pins_survive_for_the_connection_lifetime_and_release_at_close(closed_owner, component):
    root, policy, expected, ancestor = closed_owner
    target = {"database": root / "owner.sqlite3", "root": root, "parent": root.parent, "ancestor": ancestor}[component]
    store = storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    try:
        assert_rename_denied(target)
        assert store.load() == expected
    finally:
        store.close()
    displaced = target.with_name(target.name + ".synthetic-after-close")
    target.rename(displaced)
    displaced.rename(target)


@windows_only
@pytest.mark.parametrize("component", ["certificate", "key", "parent", "ancestor"])
def test_tls_loader_cannot_replace_a_pinned_component_before_openssl_load(private_tls, monkeypatch, component):
    certificate, key, client, leaf, ancestor = private_tls
    target = {"certificate": certificate, "key": key, "parent": certificate.parent, "ancestor": ancestor}[component]
    real_load = ssl.SSLContext.load_cert_chain
    attempts = []

    def load(context, *args, **kwargs):
        assert_rename_denied(target)
        attempts.append(target)
        return real_load(context, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", load)
    config = https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), certificate, key, enabled=True)
    assert attempts == [target]
    assert client.verify_mode == ssl.CERT_REQUIRED and client.check_hostname
    assert https._verify_handshake(config.ssl, client) == leaf


@windows_only
def test_a_different_valid_handshake_leaf_is_rejected(private_tls, certificates, monkeypatch):
    certificate, key, client, expected_leaf, _ = private_tls
    substitute_certificate, substitute_key, _ = certificates("substitute")
    substitute_leaf = x509.load_pem_x509_certificate(substitute_certificate.read_bytes()).public_bytes(Encoding.DER)
    assert substitute_leaf != expected_leaf
    # Establish that the alternate identity also passes this strict synthetic CA/hostname check.
    other = https.prepare_bootstrap(
        LocalOwnerPolicy(ORIGIN, HANDLE), substitute_certificate, substitute_key, enabled=True
    )
    assert https._verify_handshake(other.ssl, client) == substitute_leaf
    real_load = ssl.SSLContext.load_cert_chain
    real_handshake = https._verify_handshake
    peers = []

    def substitute(context, *_args, **kwargs):
        return real_load(context, substitute_certificate, substitute_key, **kwargs)

    def handshake(server, verifier, **kwargs):
        peer = real_handshake(server, verifier, **kwargs)
        peers.append(peer)
        return peer

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", substitute)
    monkeypatch.setattr(https, "_verify_handshake", handshake)
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), certificate, key, enabled=True)
    assert peers == [substitute_leaf]


@windows_only
def test_preexisting_database_hardlink_is_refused_before_sqlite_open(closed_owner, monkeypatch):
    root, policy, _, _ = closed_owner
    link = root / "synthetic-database-hardlink"
    make_hardlink(root / "owner.sqlite3", link)
    monkeypatch.setattr(storage.sqlite3, "connect", lambda *_a, **_k: pytest.fail("hardlinked database was opened"))
    try:
        with pytest.raises(storage.StoreUnavailable):
            storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    finally:
        link.unlink()


@windows_only
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_ambiguous_existing_sidecar_is_refused_before_sqlite_open(closed_owner, monkeypatch, suffix):
    root, policy, _, _ = closed_owner
    sidecar = root / ("owner.sqlite3" + suffix)
    sidecar.write_bytes(b"synthetic ambiguous sidecar")
    os.chmod(sidecar, 0o600)
    monkeypatch.setattr(
        storage.sqlite3, "connect", lambda *_a, **_k: pytest.fail("ambiguous SQLite sidecar was opened")
    )
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    assert sidecar.read_bytes() == b"synthetic ambiguous sidecar"


@windows_only
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_even_empty_wal_or_shared_memory_sidecar_is_refused_before_sqlite_open(closed_owner, monkeypatch, suffix):
    root, policy, _, _ = closed_owner
    sidecar = root / ("owner.sqlite3" + suffix)
    sidecar.touch(mode=0o600)
    monkeypatch.setattr(
        storage.sqlite3, "connect", lambda *_a, **_k: pytest.fail("unexpected SQLite sidecar was opened")
    )
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    assert sidecar.stat().st_size == 0


@windows_only
def test_empty_private_rollback_journal_allows_reopening_the_same_committed_state(closed_owner):
    root, policy, expected, _ = closed_owner
    journal = root / "owner.sqlite3-journal"
    journal.touch(mode=0o600)
    store = storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    try:
        assert store.load() == expected
    finally:
        store.close()


@windows_only
@pytest.mark.parametrize("component", ["certificate", "key"])
def test_preexisting_tls_hardlink_is_refused_before_openssl_load(private_tls, monkeypatch, component):
    certificate, key, _, _, _ = private_tls
    target = certificate if component == "certificate" else key
    link = target.with_name("synthetic-hardlink-" + target.name)
    make_hardlink(target, link)
    monkeypatch.setattr(
        ssl.SSLContext, "load_cert_chain", lambda *_a, **_k: pytest.fail("hardlinked TLS asset was loaded")
    )
    try:
        with pytest.raises(https.BootstrapRejected):
            https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), certificate, key, enabled=True)
    finally:
        link.unlink()


@windows_only
def test_real_junction_ancestor_is_refused_before_sqlite_open(closed_owner, junction, monkeypatch):
    root, policy, _, ancestor = closed_owner
    alias = junction(ancestor)
    through_junction = alias / root.relative_to(ancestor)
    monkeypatch.setattr(storage.sqlite3, "connect", lambda *_a, **_k: pytest.fail("junction database was opened"))
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(through_junction, policy, PRINCIPAL)
    assert root.is_dir()


@windows_only
def test_real_junction_ancestor_is_refused_before_openssl_load(private_tls, junction, monkeypatch):
    certificate, key, _, _, ancestor = private_tls
    alias = junction(ancestor)
    monkeypatch.setattr(
        ssl.SSLContext, "load_cert_chain", lambda *_a, **_k: pytest.fail("junction TLS assets were loaded")
    )
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(
            LocalOwnerPolicy(ORIGIN, HANDLE),
            alias / certificate.relative_to(ancestor),
            alias / key.relative_to(ancestor),
            enabled=True,
        )
    assert certificate.is_file() and key.is_file()


@windows_only
@pytest.mark.parametrize("api_name", ["CreateFileW", "GetFileInformationByHandle", "GetVolumeInformationW"])
def test_unavailable_windows_custody_api_never_falls_back_to_path_checks(
    closed_owner, private_tls, monkeypatch, api_name
):
    root, policy, _, _ = closed_owner
    certificate, key, _, _, _ = private_tls

    def unavailable(*_args):
        raise OSError("synthetic unavailable Windows custody API")

    monkeypatch.setattr(custody.kernel, api_name, unavailable)
    monkeypatch.setattr(storage.sqlite3, "connect", lambda *_a, **_k: pytest.fail("custody failed before SQLite open"))
    monkeypatch.setattr(
        ssl.SSLContext, "load_cert_chain", lambda *_a, **_k: pytest.fail("custody failed before OpenSSL load")
    )
    with pytest.raises(storage.StoreUnavailable) as database_error:
        storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    with pytest.raises(https.BootstrapRejected) as tls_error:
        https.prepare_bootstrap(policy, certificate, key, enabled=True)
    assert str(database_error.value) == str(tls_error.value) == ""


def test_unsupported_platform_refuses_before_creating_or_opening_assets(tmp_path, certificates, monkeypatch):
    certificate, key, _ = certificates()
    root = tmp_path / "synthetic-unsupported-owner"
    root.mkdir(mode=0o700)
    policy = LocalOwnerPolicy(ORIGIN, HANDLE)
    monkeypatch.setattr(storage, "current_principal", lambda: PRINCIPAL)
    monkeypatch.setattr(custody.sys, "platform", "unsupported-synthetic-platform")
    monkeypatch.setattr(storage.sqlite3, "connect", lambda *_a, **_k: pytest.fail("unsupported SQLite open"))
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", lambda *_a, **_k: pytest.fail("unsupported OpenSSL load"))
    with pytest.raises(custody.CustodyError):
        custody.Custody(root)
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(root, policy, PRINCIPAL)
    uncreated = tmp_path / "synthetic-unsupported-provision"
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore.provision(
            uncreated, policy, PRINCIPAL, synthetic_only=True, authorize=lambda: True
        )
    assert not uncreated.exists() and not (root / "owner.sqlite3").exists()
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(policy, certificate, key, enabled=True)
