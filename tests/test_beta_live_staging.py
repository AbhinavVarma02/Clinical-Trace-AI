"""Offline contract for the immutable LIVE allowlist and the private staging/erasure lifecycle (synthetic bytes)."""

from __future__ import annotations

import dataclasses
import os

import pytest

from src.interop.epic import beta_live_staging as staging_module
from src.interop.epic.beta_acquisition import AcquisitionError
from src.interop.epic.beta_catalog import Catalog
from src.interop.epic.beta_live_staging import (
    MAX_CANDIDATE_BYTES,
    STAGING_RETENTION_S,
    LiveAllowlist,
    LiveStaging,
    private_root,
)

ALIAS = "approved-alpha"
MARK = b"invented-synthetic-bundle-marker"


def attempt(n: int) -> str:
    return f"attempt-{n:020d}"


class Clock:
    now = 1_799_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def catalog(tmp_path, clock):
    value = Catalog(tmp_path / "control.sqlite", clock)
    yield value
    value.close()


@pytest.fixture
def store(tmp_path, catalog):
    return LiveStaging(catalog, tmp_path / "live-staging")


def put(store, n=1, alias=ALIAS, *, ok=lambda: True, bundle=MARK * 8):
    store.stage(attempt(n), alias, b'{"manifest":"invented"}', bundle, ok)


def rows(catalog):
    return {r["attempt_id"]: dict(r) for r in catalog.db.execute("SELECT * FROM live_candidate")}


def entries(store):
    return sorted(p.name for p in store.root.iterdir())


# -- allowlist ---------------------------------------------------------------------------------------------------


def test_allowlist_is_empty_by_default_and_holds_at_most_one_patient():
    assert dict(LiveAllowlist.empty().patients) == {}
    assert LiveAllowlist({ALIAS: "invented-live-alpha"}).get(ALIAS) == "invented-live-alpha"
    with pytest.raises(ValueError):
        LiveAllowlist({ALIAS: "invented-live-alpha", "approved-bravo": "invented-live-bravo"})


@pytest.mark.parametrize(
    "alias,patient",
    [
        ("mock-alpha", "invented-live-alpha"),  # MOCK alias
        (ALIAS, "invented-patient-alpha"),  # MOCK patient id
        (ALIAS, "."),
        (ALIAS, ".."),
        (ALIAS, "../other"),
        (ALIAS, ""),
        ("Bad Alias", "invented-live-alpha"),
        ("x", "invented-live-alpha"),
    ],
)
def test_allowlist_rejects_unsafe_or_mock_identities(alias, patient):
    with pytest.raises(ValueError):
        LiveAllowlist({alias: patient})


def test_allowlist_is_immutable_and_detached_from_its_source():
    source = {ALIAS: "invented-live-alpha"}
    allowlist = LiveAllowlist(source)
    source["approved-bravo"] = "invented-live-bravo"
    source[ALIAS] = "changed"
    assert dict(allowlist.patients) == {ALIAS: "invented-live-alpha"}
    with pytest.raises(TypeError):
        allowlist.patients["approved-bravo"] = "invented-live-bravo"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        allowlist.patients = {}  # type: ignore[misc]


# -- private root ------------------------------------------------------------------------------------------------


def test_root_inside_a_git_work_tree_is_refused(tmp_path):
    (tmp_path / "work" / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="Git"):
        private_root(tmp_path / "work" / "live-staging")
    with pytest.raises(ValueError, match="Git"):
        private_root(tmp_path / "work" / "nested" / "deeper")


def dir_link(link, target):
    """A directory symlink, or a junction on Windows accounts that may not create symlinks."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlink creation is not permitted here")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def test_root_that_is_a_link_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    dir_link(tmp_path / "linked", target)
    with pytest.raises(ValueError, match="link"):
        private_root(tmp_path / "linked")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only; Windows is covered by the DACL verifier tests")
def test_posix_modes_are_private(store):
    put(store)
    assert (store.root.stat().st_mode & 0o777) == 0o700
    final = store.root / attempt(1)
    assert (final.stat().st_mode & 0o777) == 0o700
    assert all((f.stat().st_mode & 0o777) == 0o600 for f in final.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows location policy")
def test_windows_root_must_be_in_the_user_profile_or_temp(tmp_path, monkeypatch):
    assert private_root(tmp_path / "ok")  # the pytest temp directory is inside the user's temp directory
    monkeypatch.setattr(staging_module.tempfile, "gettempdir", lambda: str(tmp_path / "other-temp"))
    monkeypatch.setattr(staging_module.Path, "home", classmethod(lambda cls: tmp_path / "other-home"))
    with pytest.raises(ValueError, match="profile or temp"):
        private_root(tmp_path / "second")


# -- lifecycle ---------------------------------------------------------------------------------------------------


def test_stage_writes_two_files_and_a_content_free_row(store, catalog):
    put(store)
    assert entries(store) == [attempt(1)]
    assert sorted(p.name for p in (store.root / attempt(1)).iterdir()) == ["manifest.json", "normalized_bundle.json"]
    assert (store.root / attempt(1) / "normalized_bundle.json").read_bytes() == MARK * 8
    row = rows(catalog)[attempt(1)]
    assert row["state"] == "staged" and row["bytes"] == len(b'{"manifest":"invented"}') + len(MARK * 8)
    view = store.view(ALIAS)
    assert set(view) == {"attempt_id", "state", "bytes", "created_at", "expires_at", "manifest_sha256", "review"}
    assert view["expires_at"] - view["created_at"] == STAGING_RETENTION_S
    # The catalog holds no candidate content.
    assert MARK.decode() not in "\n".join(catalog.db.iterdump())


def test_retention_is_enforced_at_exactly_24_hours(store, catalog, clock):
    put(store)
    clock.now += STAGING_RETENTION_S - 1
    assert store.purge() == 0 and entries(store) == [attempt(1)]
    clock.now += 1
    assert store.purge() == 1 and entries(store) == []
    row = rows(catalog)[attempt(1)]
    assert (row["state"], row["manifest_sha256"], row["bytes"], row["erase_reason"]) == (
        "erased",
        None,
        None,
        "expired",
    )
    assert store.view(ALIAS) is None


def test_a_newer_candidate_supersedes_the_older_one(store, catalog):
    put(store, 1)
    put(store, 2)
    assert entries(store) == [attempt(2)]
    state = {k: v["state"] for k, v in rows(catalog).items()}
    assert state == {attempt(1): "erased", attempt(2): "staged"}
    assert rows(catalog)[attempt(1)]["erase_reason"] == "superseded"


def test_owner_requested_erasure_and_erase_all(store, catalog):
    put(store)
    assert store.erase_alias(ALIAS, "owner_requested") == 1
    assert entries(store) == [] and store.erase_alias(ALIAS, "owner_requested") == 0
    put(store, 2)
    assert store.erase_all("owner_revoked") == 1 and entries(store) == []


def test_revocation_erases_on_the_next_purge_and_blocks_new_candidates(store, catalog):
    put(store)
    catalog.revoke(ALIAS)
    assert store.purge() == 1 and entries(store) == []
    assert rows(catalog)[attempt(1)]["erase_reason"] == "revoked"
    with pytest.raises(AcquisitionError, match="authorization_revoked"):
        put(store, 2)
    assert entries(store) == [] and attempt(2) not in rows(catalog)


def test_owner_revocation_erases_every_candidate(store, catalog):
    put(store)
    catalog.revoke("owner")
    assert store.purge() == 1 and entries(store) == []
    assert rows(catalog)[attempt(1)]["erase_reason"] == "owner_revoked"
    with pytest.raises(AcquisitionError, match="authorization_revoked"):
        put(store, 2)


def test_authorization_lost_at_the_last_boundary_leaves_nothing(store, catalog):
    answers = iter([True, False])
    with pytest.raises(AcquisitionError, match="authorization_revoked"):
        put(store, ok=lambda: next(answers))
    assert entries(store) == []
    assert rows(catalog)[attempt(1)]["state"] == "erased"


def test_write_failure_leaves_nothing_and_a_generic_error(store, catalog, monkeypatch):
    real = staging_module._write
    calls = []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("invented disk failure with /secret/path")
        real(path, data)

    monkeypatch.setattr(staging_module, "_write", flaky)
    with pytest.raises(AcquisitionError) as error:
        put(store)
    assert str(error.value) == "staging_failed" and "secret" not in str(error.value)
    assert entries(store) == [] and rows(catalog)[attempt(1)]["erase_reason"] == "write_failed"


def test_a_failed_erasure_is_visible_and_retried(store, catalog, monkeypatch):
    put(store)
    real = staging_module._remove
    monkeypatch.setattr(staging_module, "_remove", lambda path: False)
    assert store.erase_alias(ALIAS, "revoked") == 0
    assert store.view(ALIAS)["state"] == "erase_pending" and entries(store) == [attempt(1)]
    codes = [r["code"] for r in catalog.db.execute("SELECT code FROM audit")]
    assert "live_erase_failed" in codes
    monkeypatch.setattr(staging_module, "_remove", real)
    assert store.purge() == 1 and entries(store) == []
    assert rows(catalog)[attempt(1)]["erase_reason"] == "retry"


def test_oversized_and_malformed_requests_write_nothing(store, catalog, tmp_path):
    with pytest.raises(AcquisitionError, match="byte_limit"):
        put(store, bundle=b"x" * (MAX_CANDIDATE_BYTES + 1))
    for bad in ("../escape", "short", "a/b" + "c" * 20, "x" * 20 + ".tmp"):
        with pytest.raises(AcquisitionError, match="staging_scope"):
            store.stage(bad, ALIAS, b"{}", b"{}", lambda: True)
    with pytest.raises(AcquisitionError, match="staging_scope"):
        store.stage(attempt(3), "Bad Alias", b"{}", b"{}", lambda: True)
    assert entries(store) == [] and rows(catalog) == {}
    assert not (tmp_path / "escape").exists()


def test_erasure_never_follows_a_link_out_of_the_candidate(store, tmp_path):
    put(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("must survive")
    dir_link(store.root / attempt(1) / "link", outside)
    assert store.erase_alias(ALIAS, "owner_requested") == 1
    assert entries(store) == [] and canary.read_text() == "must survive"


def test_restart_sweep_applies_retention_revocation_and_removes_orphans(tmp_path, catalog, clock):
    first = LiveStaging(catalog, tmp_path / "live-staging")
    put(first, 1)  # will expire
    clock.now += STAGING_RETENTION_S - 100
    put(first, 2, alias="approved-bravo")  # still within retention
    # Simulate a crash mid-write and stray files.
    (first.root / f"{attempt(9)}.tmp").mkdir()
    (first.root / f"{attempt(9)}.tmp" / "normalized_bundle.json").write_bytes(MARK)
    (first.root / "unknown.txt").write_text("not ours")
    catalog.db.execute(
        "INSERT INTO live_candidate (attempt_id, alias, state, created_at, expires_at) VALUES (?, ?, 'writing', ?, ?)",
        (attempt(9), "approved-gamma", clock.now, clock.now + STAGING_RETENTION_S),
    )
    catalog.db.commit()
    clock.now += 200  # attempt 1 is now past 24 h
    second = LiveStaging(catalog, tmp_path / "live-staging")
    assert entries(second) == [attempt(2)]
    state = {k: (v["state"], v["erase_reason"]) for k, v in rows(catalog).items()}
    assert state[attempt(1)] == ("erased", "expired")
    assert state[attempt(9)] == ("erased", "interrupted")
    assert state[attempt(2)][0] == "staged"
    catalog.revoke("approved-bravo")
    third = LiveStaging(catalog, tmp_path / "live-staging")
    assert entries(third) == [] and rows(catalog)[attempt(2)]["erase_reason"] == "revoked"


def test_catalog_and_audit_stay_content_free(store, catalog):
    put(store)
    store.erase_alias(ALIAS, "owner_requested")
    codes = {r["code"] for r in catalog.db.execute("SELECT code FROM audit")}
    assert {"live_candidate_staged", "live_candidate_erased"} <= codes
    dump = "\n".join(catalog.db.iterdump())
    assert MARK.decode() not in dump and "invented" not in dump
