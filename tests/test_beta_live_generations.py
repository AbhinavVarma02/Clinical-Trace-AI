"""Offline contract for evidence-only LIVE generations and the Windows/POSIX private-storage gate.

Fixtures are synthetic. A test-only subclass labels fake-transport data as LIVE-tier so the store can be exercised;
production code has no switch that does so. No real Epic request, credential or LIVE patient is involved.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from src.interop.epic import beta_live_staging as files
from src.interop.epic import settings
from src.interop.epic.beta_acquisition import AcquisitionError
from src.interop.epic.beta_catalog import LIVE_POLICY_VERSION, Catalog, RefreshRejected
from src.interop.epic.beta_live_acquisition import LiveAcquisition
from src.interop.epic.beta_live_generations import GENERATION_RETENTION_S, LiveGenerations
from src.interop.epic.beta_live_staging import STAGING_RETENTION_S, LiveAllowlist, LiveStaging
from src.interop.epic.beta_staging import encoded
from src.interop.epic.mapping import content_sha256, internal_id
from tests.test_beta_generations import BODY
from tests.test_beta_live_route import (
    LIVE,
    LIVE_ALIAS,
    LIVE_PATIENT,
    FakeEpic,
    build,
    fake as fake_fixture,
    refresh_and_wait,
    step_up,
    staged,
)
from tests.test_beta_owner_refresh import ALPHA, harness as owner_fixture, keys as keys_fixture

harness = owner_fixture
keys = keys_fixture
fake = fake_fixture

OTHER_PATIENT = "invented-live-bravo"


class FixtureLive(LiveAcquisition):
    """Test-only: label fake-transport data as LIVE-tier so the LIVE store can be exercised offline."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode, self.source = "LIVE", settings.SOURCE_SYSTEM


class Clock:
    now = 1_799_000_000.0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self, tmp_path, key, epic):
        self.root = tmp_path
        self.clock, self.key, self.epic, self.n = Clock(), key, epic, 0
        self.catalog = Catalog(tmp_path / "control.sqlite", self.clock)
        self.allow = LiveAllowlist({LIVE_ALIAS: LIVE_PATIENT})
        self.staging = LiveStaging(self.catalog, tmp_path / "live-staging")
        self.hooks, self.ok, self.fresh = [], True, True
        self.gens = self.generations()

    def generations(self):
        return LiveGenerations(self.catalog, self.root / "live-generations", self.allow, self.staging, hook=self.hook)

    def hook(self, point):
        for callback in self.hooks:
            callback(point)

    def candidate(self, cls=FixtureLive, mutate=None):
        """Run the adapter against the fake Epic and stage its candidate. Returns (attempt, manifest hash)."""
        self.n += 1
        attempt, _ = self.catalog.admit(
            LIVE_ALIAS,
            f"idempotency-key-{self.n:04d}",
            cooldown_s=0,
            patient_limit=99,
            global_limit=99,
            request_limit=10_000,
            policy=LIVE_POLICY_VERSION,
        )
        self.catalog.start(attempt)
        config = settings.EpicConfig("invented-live-client", Path("never-read.key"), "https://keys.invalid/jwks")
        adapter = cls(
            self.catalog,
            attempt,
            LIVE_ALIAS,
            self.key,
            config,
            allowlist=self.allow,
            enabled=True,
            authorized=lambda: True,
            transport=httpx.MockTransport(self.epic.respond),
        )
        asyncio.run(adapter.run())
        self.catalog.finish(attempt, "succeeded", operations=adapter.report())
        manifest, bundle = adapter.candidate
        if mutate is not None:
            mutate(manifest)
        data = encoded(manifest)
        self.staging.stage(attempt, LIVE_ALIAS, data, bundle, lambda: True)
        return attempt, content_sha256(data)

    def approve(self, attempt, digest, alias=LIVE_ALIAS):
        return self.gens.approve(alias, attempt, digest, lambda: self.ok, lambda: self.fresh)

    def objects(self):
        return sorted(p.name for p in (self.root / "live-generations" / "objects").iterdir())

    def temporary(self):
        return sorted(p.name for p in (self.root / "live-generations" / "tmp").iterdir())

    def rows(self):
        return {r["generation_id"]: dict(r) for r in self.catalog.db.execute("SELECT * FROM live_generation")}

    def head(self):
        return dict(self.catalog.db.execute("SELECT * FROM patient_head WHERE alias = ?", (LIVE_ALIAS,)).fetchone())


@pytest.fixture
def world(tmp_path, monkeypatch, keys):
    monkeypatch.setenv(settings.ENABLE_ENV, "1")
    value = World(tmp_path, keys[1], FakeEpic())
    yield value
    value.catalog.close()


def refused(world, attempt, digest, status, code):
    with pytest.raises(RefreshRejected) as error:
        world.approve(attempt, digest)
    assert (error.value.status, error.value.code) == (status, code)


def nothing_written(world):
    assert world.objects() == [] and world.temporary() == [] and world.rows() == {}


# -- the approved generation ---------------------------------------------------------------------------------


def test_approval_creates_an_evidence_only_generation_and_consumes_the_candidate(world):
    world.epic.outcomes = {"Condition:encounter-diagnosis": "4101", "DocumentReference:clinical-note": "59100"}
    attempt, digest = world.candidate()
    view = world.approve(attempt, digest)
    gid = view["generation_id"]
    assert view["state"] == "approved" and view["freshness"] == "fresh"
    assert view["expires_at"] - view["approved_at"] == GENERATION_RETENTION_S
    review = view["review"]
    assert (review["mode"], review["source"], review["completeness"]) == ("LIVE", settings.SOURCE_SYSTEM, "unconfirmed")
    assert review["operations"]["DocumentReference:clinical-note"]["informational_codes"] == ["59100"]
    assert review["operations"]["Condition:encounter-diagnosis"]["informational_codes"] == ["4101"]
    assert world.objects() == [gid] and world.temporary() == []
    assert sorted(p.name for p in (world.root / "live-generations" / "objects" / gid).iterdir()) == [
        "manifest.json",
        "normalized_bundle.json",
    ]
    # Evidence only: no runtime state, database or journal anywhere in the store.
    assert not [p for p in (world.root / "live-generations").rglob("*") if "sqlite" in p.name]
    # The candidate was consumed and the pointer moved atomically.
    assert world.staging.view(LIVE_ALIAS) is None and staged_names(world) == []
    assert world.head()["active"] == gid and world.head()["last_success"] == gid
    assert [r["state"] for r in world.rows().values()] == ["approved"]
    # The description is content-free.
    text = json.dumps(view)
    assert all(
        secret not in text for secret in (LIVE_PATIENT, BODY, "Binary/secret", internal_id("Patient", LIVE_PATIENT))
    )


def staged_names(world):
    return sorted(p.name for p in world.staging.root.iterdir())


def test_the_candidate_manifest_carries_warnings_completeness_and_provenance(world):
    world.epic.outcomes = {"DocumentReference:clinical-note": "59100"}
    attempt, _ = world.candidate()
    _, manifest_bytes, _ = world.staging.read_candidate(attempt, LIVE_ALIAS)
    manifest = json.loads(manifest_bytes)
    assert manifest["completeness"] == "unconfirmed" and manifest["synthetic_transport"] is True
    assert manifest["coverage"]["operations"]["DocumentReference:clinical-note"]["informational_codes"] == ["59100"]


# -- tier isolation ------------------------------------------------------------------------------------------

GONE = object()


def set_field(name, value):
    def mutate(manifest):
        if value is GONE:
            manifest.pop(name)
        else:
            manifest[name] = value

    return mutate


MUTATIONS = {
    "mode": set_field("mode", "MOCK"),
    "source": set_field("source", "epic-sandbox-mock"),
    "policy": set_field("policy", "mock-initial-r4-v1"),
    "manifest_version": set_field("manifest_version", "mock-generation/1"),
    "mapping_version": set_field("mapping_version", "other"),
    "completeness_other": set_field("completeness", "complete"),
    "completeness_missing": set_field("completeness", GONE),
    "alias": set_field("alias", "approved-bravo"),
    "patient": set_field("patient_id", internal_id("Patient", OTHER_PATIENT)),
    "bundle_hash": set_field("bundle_sha256", "0" * 64),
    "generation_id": set_field("generation_id", "not-a-generation-id"),
    "provenance_type": set_field("synthetic_transport", "yes"),
    "provenance_missing": set_field("synthetic_transport", GONE),
}


@pytest.mark.parametrize("mutate", list(MUTATIONS.values()), ids=list(MUTATIONS))
def test_a_manifest_that_is_not_exactly_this_tier_is_never_approved(world, mutate):
    previous_attempt, previous_digest = world.candidate()
    first = world.approve(previous_attempt, previous_digest)["generation_id"]
    attempt, digest = world.candidate(mutate=mutate)
    refused(world, attempt, digest, 409, "candidate_not_approvable")
    assert world.objects() == [first] and world.temporary() == []
    assert world.head()["active"] == first and world.gens.view(LIVE_ALIAS)["generation_id"] == first


def test_a_fake_transport_candidate_labelled_mock_can_never_enter_the_live_store(world):
    attempt, digest = world.candidate(cls=LiveAcquisition)  # production class: fake transport => MOCK label
    manifest = json.loads(world.staging.read_candidate(attempt, LIVE_ALIAS)[1])
    assert (manifest["mode"], manifest["source"]) == ("MOCK", "epic-sandbox-mock")
    refused(world, attempt, digest, 409, "candidate_not_approvable")
    nothing_written(world)


def test_real_data_cannot_be_promoted_until_genuine_authentication_exists(world):
    attempt, digest = world.candidate(mutate=set_field("synthetic_transport", False))
    assert world.gens.real_promotion is False
    refused(world, attempt, digest, 409, "real_promotion_disabled")
    nothing_written(world)


def test_live_aliases_and_mock_aliases_stay_apart(world):
    attempt, digest = world.candidate()
    with pytest.raises(RefreshRejected) as error:
        world.gens.approve("mock-alpha", attempt, digest, lambda: True, lambda: True)
    assert (error.value.status, error.value.code) == (404, "patient_unavailable")
    nothing_written(world)


# -- approval preconditions ----------------------------------------------------------------------------------


def test_approval_binds_to_the_attempt_and_the_reviewed_hash(world):
    attempt, digest = world.candidate()
    refused(world, "unknown-attempt-0000000000", digest, 404, "candidate_unavailable")
    refused(world, attempt, "f" * 64, 409, "candidate_hash_mismatch")
    refused(world, attempt, "", 400, "candidate_hash_required")
    refused(world, attempt, "not-a-hash", 400, "candidate_hash_required")
    nothing_written(world)
    assert world.approve(attempt, digest)["state"] == "approved"
    refused(world, attempt, digest, 404, "candidate_unavailable")  # replay: the candidate was consumed


def test_a_newer_candidate_makes_the_older_approval_impossible(world):
    old_attempt, old_digest = world.candidate()
    new_attempt, new_digest = world.candidate()
    refused(world, old_attempt, old_digest, 404, "candidate_unavailable")
    assert world.approve(new_attempt, new_digest)["state"] == "approved"


@pytest.mark.parametrize(
    "change,status,code",
    [
        ("not_fresh", 401, "step_up_required"),
        ("unauthorized", 401, "owner_required"),
        ("alias_revoked", 404, "patient_unavailable"),
        ("owner_revoked", 404, "patient_unavailable"),
        ("expired", 410, "candidate_expired"),
        ("fence", 409, "publication_fenced"),
        ("bundle_tampered", 409, "candidate_integrity"),
        ("manifest_tampered", 409, "candidate_integrity"),
    ],
)
def test_every_precondition_fails_closed_without_leaving_anything(world, change, status, code):
    attempt, digest = world.candidate()
    directory = world.staging.root / attempt
    if change == "not_fresh":
        world.fresh = False
    elif change == "unauthorized":
        world.ok = False
    elif change == "alias_revoked":
        world.catalog.revoke(LIVE_ALIAS)
    elif change == "owner_revoked":
        world.catalog.revoke("owner")
    elif change == "expired":
        world.clock.now += STAGING_RETENTION_S + 1
    elif change == "fence":
        world.catalog.db.execute("UPDATE patient_head SET epoch = epoch + 1 WHERE alias = ?", (LIVE_ALIAS,))
        world.catalog.db.commit()
    elif change == "bundle_tampered":
        (directory / "normalized_bundle.json").write_bytes(b"{}")
    else:
        (directory / "manifest.json").write_bytes(b"{}")
    refused(world, attempt, digest, status, code)
    nothing_written(world)


def test_unverifiable_private_storage_blocks_promotion_with_nothing_written(world, monkeypatch):
    attempt, digest = world.candidate()

    def deny(path):
        raise AcquisitionError("acl_unverified")

    monkeypatch.setattr(files, "verify_private_path", deny)
    refused(world, attempt, digest, 503, "acl_unverified")
    nothing_written(world)


# -- atomic, failure-safe replacement ------------------------------------------------------------------------


@pytest.mark.parametrize("point", ["after_check", "after_tmp", "after_rename", "before_commit"])
def test_a_failure_before_the_commit_leaves_the_previous_generation_active_and_intact(world, point):
    first_attempt, first_digest = world.candidate()
    first = world.approve(first_attempt, first_digest)["generation_id"]
    attempt, digest = world.candidate()

    def explode(name):
        if name == point:
            raise RuntimeError("invented failure with /secret/path")

    world.hooks.append(explode)
    with pytest.raises((RuntimeError, RefreshRejected)):
        world.approve(attempt, digest)
    world.hooks.clear()
    assert world.objects() == [first] and world.temporary() == []
    assert world.head()["active"] == first
    assert [r["state"] for r in world.rows().values()] == ["approved"]
    assert world.gens.view(LIVE_ALIAS)["generation_id"] == first and world.gens.view(LIVE_ALIAS)["review"]
    assert world.staging.view(LIVE_ALIAS)["attempt_id"] == attempt  # the candidate survives, so a retry is possible
    second = world.approve(attempt, digest)["generation_id"]
    assert second != first and world.objects() == [second] and world.head()["active"] == second
    assert sorted(r["state"] for r in world.rows().values()) == ["approved", "erased"]


def test_a_failed_pointer_update_rolls_the_whole_transaction_back(world, monkeypatch):
    first_attempt, first_digest = world.candidate()
    first = world.approve(first_attempt, first_digest)["generation_id"]
    attempt, digest = world.candidate()
    # The fence check passes but the guarded pointer UPDATE matches no row: insert and demotion must both roll back.
    monkeypatch.setattr(world.gens, "_fence", lambda a, b: (10_000, 10_000))
    refused(world, attempt, digest, 409, "publication_fenced")
    assert world.objects() == [first] and world.temporary() == []
    assert world.head()["active"] == first
    assert [r["state"] for r in world.rows().values()] == ["approved"]
    assert world.staging.view(LIVE_ALIAS)["attempt_id"] == attempt


def test_a_failure_after_the_commit_keeps_the_new_generation_and_restart_finishes_the_cleanup(world):
    first_attempt, first_digest = world.candidate()
    first = world.approve(first_attempt, first_digest)["generation_id"]
    attempt, digest = world.candidate()

    def explode(name):
        if name == "after_commit":
            raise RuntimeError("invented failure")

    world.hooks.append(explode)
    with pytest.raises(RuntimeError):
        world.approve(attempt, digest)
    world.hooks.clear()
    states = {gid: row["state"] for gid, row in world.rows().items()}
    assert states[first] == "erase_pending" and list(states.values()).count("approved") == 1
    new = next(gid for gid, state in states.items() if state == "approved")
    assert world.head()["active"] == new
    restarted = world.generations()  # startup sweep: erase the superseded generation and the promoted candidate
    assert world.objects() == [new] and restarted.view(LIVE_ALIAS)["generation_id"] == new
    assert world.rows()[first]["state"] == "erased" and world.rows()[first]["erase_reason"] == "superseded"
    assert staged_names(world) == []


def test_only_one_generation_can_ever_be_approved_per_alias(world):
    attempt, digest = world.candidate()
    world.approve(attempt, digest)
    with pytest.raises(Exception, match="UNIQUE|constraint"):
        world.catalog.db.execute(
            "INSERT INTO live_generation (generation_id, alias, attempt_id, state, approved_at, expires_at) "
            "VALUES ('a' || hex(randomblob(15)), ?, 'other-attempt-id-000', 'approved', 0, 1)",
            (LIVE_ALIAS,),
        )


def test_a_failed_erase_of_the_superseded_generation_is_retried_and_never_blocks_the_new_one(world, monkeypatch):
    first_attempt, first_digest = world.candidate()
    first = world.approve(first_attempt, first_digest)["generation_id"]
    attempt, digest = world.candidate()
    real = files._remove
    monkeypatch.setattr(files, "_remove", lambda path: False if first in str(path) else real(path))
    second = world.approve(attempt, digest)["generation_id"]
    assert world.head()["active"] == second and world.rows()[first]["state"] == "erase_pending"
    assert world.gens.view(LIVE_ALIAS)["generation_id"] == second
    monkeypatch.setattr(files, "_remove", real)
    assert world.gens.purge() >= 1 and world.objects() == [second]
    assert world.rows()[first]["state"] == "erased"


# -- S4: a real process killed at each promotion boundary ------------------------------------------------------

KILL_CHILD = """
from tests.offline_network import install
install()
import sys
import time
from pathlib import Path
from src.interop.epic.beta_catalog import Catalog
from src.interop.epic.beta_live_generations import LiveGenerations
from src.interop.epic.beta_live_staging import LiveAllowlist, LiveStaging
root, now, alias, patient, attempt, digest, target, marker = sys.argv[1:]
root = Path(root)
catalog = Catalog(root / "control.sqlite", lambda: float(now))
allowlist = LiveAllowlist({alias: patient})
staging = LiveStaging(catalog, root / "live-staging")
def hook(point):
    if point == target:
        pending = Path(marker).with_suffix(".tmp")
        pending.write_text(point)
        pending.replace(marker)
        time.sleep(120)  # the parent terminates this process here, with no chance to clean up
LiveGenerations(catalog, root / "live-generations", allowlist, staging, hook=hook).approve(
    alias, attempt, digest, lambda: True, lambda: True
)
"""

KILL_POINTS = ["after_check", "after_tmp", "after_rename", "before_commit", "after_commit"]


def tree_hashes(directory):
    return {p.name: content_sha256(p.read_bytes()) for p in sorted(directory.iterdir())}


@pytest.mark.parametrize("point", KILL_POINTS)
def test_a_killed_process_leaves_the_previous_generation_intact_and_restart_recovers(world, point):
    import sys

    first_attempt, first_digest = world.candidate()
    first = world.approve(first_attempt, first_digest)["generation_id"]
    before = tree_hashes(world.root / "live-generations" / "objects" / first)
    attempt, digest = world.candidate()
    _, manifest_bytes, _ = world.staging.read_candidate(attempt, LIVE_ALIAS)
    new = json.loads(manifest_bytes)["generation_id"]
    marker = world.root / "kill-marker"
    world.catalog.close()  # the writer process must be the only connection to the database
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            KILL_CHILD,
            str(world.root),
            str(world.clock.now),
            LIVE_ALIAS,
            LIVE_PATIENT,
            attempt,
            digest,
            point,
            str(marker),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        deadline = time.monotonic() + 90
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), process.communicate(timeout=1)
        assert marker.read_text() == point and process.poll() is None, "child did not stop at the intended point"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    assert process.returncode != 0

    # What the kill left on disk, before any recovery runs: proof the process really stopped at this boundary.
    committed = point == "after_commit"
    tmp, objects = world.temporary(), world.objects()
    assert (f"{new}.obj" in tmp) is (point == "after_tmp")
    assert (new in objects) is (point in {"after_rename", "before_commit", "after_commit"})
    assert not [name for name in tmp if name.endswith(".check")]  # the throwaway check directory is always gone
    assert first in objects  # nothing touched the previous generation

    # Restart: a fresh process view of the same directories.
    catalog = Catalog(world.root / "control.sqlite", world.clock)
    try:
        staging = LiveStaging(catalog, world.root / "live-staging")
        gens = LiveGenerations(catalog, world.root / "live-generations", world.allow, staging)
        rows = {r["generation_id"]: dict(r) for r in catalog.db.execute("SELECT * FROM live_generation")}
        head = dict(catalog.db.execute("SELECT * FROM patient_head WHERE alias = ?", (LIVE_ALIAS,)).fetchone())
        objects = sorted(p.name for p in (world.root / "live-generations" / "objects").iterdir())
        assert sorted(p.name for p in (world.root / "live-generations" / "tmp").iterdir()) == []  # no orphan tmp
        assert [gid for gid, row in rows.items() if row["state"] == "approved"] == [new if committed else first]
        assert head["active"] == (new if committed else first)
        assert gens.view(LIVE_ALIAS)["generation_id"] == (new if committed else first)  # re-hashed on load
        if committed:
            assert objects == [new] and rows[first]["state"] == "erased"  # the superseded one was erased on restart
            assert staging.view(LIVE_ALIAS) is None  # the promoted candidate was erased as well
        else:
            assert objects == [first] and new not in rows  # no orphan object, nothing half-recorded
            assert tree_hashes(world.root / "live-generations" / "objects" / first) == before
            assert staging.view(LIVE_ALIAS)["attempt_id"] == attempt  # still staged, so the owner can retry
            retried = gens.approve(LIVE_ALIAS, attempt, digest, lambda: True, lambda: True)
            assert retried["generation_id"] == new and gens.view(LIVE_ALIAS)["generation_id"] == new
    finally:
        catalog.close()
    world.catalog = Catalog(
        world.root / "control.sqlite", world.clock
    )  # so the fixture teardown has something to close


# -- retention and erasure -----------------------------------------------------------------------------------


def approved(world):
    attempt, digest = world.candidate()
    return world.approve(attempt, digest)["generation_id"]


def test_thirty_day_retention_is_exact(world):
    gid = approved(world)
    world.clock.now += GENERATION_RETENTION_S - 1
    assert world.gens.purge() == 0 and world.objects() == [gid]
    world.clock.now += 1
    assert world.gens.purge() == 1 and world.objects() == []
    row = world.rows()[gid]
    assert (row["state"], row["manifest_sha256"], row["erase_reason"]) == ("erased", None, "expired")
    assert world.head()["active"] is None and world.gens.view(LIVE_ALIAS) is None


@pytest.mark.parametrize("how", ["alias_revoked", "owner_revoked", "owner_request", "erase_all"])
def test_revocation_and_owner_requests_erase_the_generation(world, how):
    gid = approved(world)
    if how == "alias_revoked":
        world.catalog.revoke(LIVE_ALIAS)
        assert world.gens.purge() == 1
    elif how == "owner_revoked":
        world.catalog.revoke("owner")
        assert world.gens.purge() == 1
    elif how == "owner_request":
        assert world.gens.erase_alias(LIVE_ALIAS, "owner_requested") == 1
    else:
        assert world.gens.erase_all("owner_revoked") == 1
    assert world.objects() == [] and world.head()["active"] is None
    assert world.rows()[gid]["state"] == "erased" and world.gens.view(LIVE_ALIAS) is None


def test_tampering_after_approval_is_detected_on_every_read(world):
    gid = approved(world)
    bundle = world.root / "live-generations" / "objects" / gid / "normalized_bundle.json"
    original = bundle.read_bytes()
    bundle.write_bytes(original + b" ")
    view = world.gens.view(LIVE_ALIAS)
    assert view["evidence_error"] == "generation_integrity" and view["review"] is None
    bundle.write_bytes(original)
    assert world.gens.view(LIVE_ALIAS)["review"]["mode"] == "LIVE"


def test_restart_sweep_removes_unowned_and_temporary_files_but_keeps_the_generation(world):
    gid = approved(world)
    store = world.root / "live-generations"
    (store / "objects" / ("b" * 32)).mkdir()
    (store / "objects" / ("b" * 32) / "normalized_bundle.json").write_bytes(b"stray")
    (store / "tmp" / ("c" * 32 + ".check")).mkdir()
    (store / "tmp" / ("c" * 32 + ".obj")).mkdir()
    world.generations()
    assert world.objects() == [gid] and world.temporary() == []


def test_no_runtime_handles_remain_so_erasure_works_on_windows(world):
    gid = approved(world)
    assert not [p for p in (world.root / "live-generations").rglob("*") if "sqlite" in p.name]
    assert (
        world.gens.erase_alias(LIVE_ALIAS, "owner_requested") == 1
        and not (world.root / "live-generations" / "objects" / gid).exists()
    )


# -- the private-storage gate --------------------------------------------------------------------------------


def widen(path):
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:(OI)(CI)(R)"], check=True, capture_output=True)
    else:
        os.chmod(path, 0o755)


def restore(path):
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/remove:g", "*S-1-1-0"], check=True, capture_output=True)
    else:
        os.chmod(path, 0o700)


def test_the_real_verifier_accepts_a_private_directory_and_refuses_a_widened_one(tmp_path):
    private = files.private_root(tmp_path / "private")
    assert files.acl_problem(private) is None
    files.verify_private_path(private)
    widen(private)
    try:
        assert files.acl_problem(private) is not None
        with pytest.raises(AcquisitionError, match="acl_unverified"):
            files.verify_private_path(private)
    finally:
        restore(private)
    assert files.acl_problem(private) is None
    with pytest.raises(AcquisitionError, match="acl_unverified"):
        files.verify_private_path(tmp_path / "does-not-exist")


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL specifics")
def test_the_windows_verifier_refuses_directories_that_other_accounts_can_use(tmp_path):
    assert files.acl_problem(Path(os.environ.get("SystemDrive", "C:") + "/")) is not None  # not user-owned


def test_unverified_storage_refuses_every_candidate_whatever_its_label(world):
    widen(world.staging.root)
    try:
        with pytest.raises(AcquisitionError, match="acl_unverified"):
            world.candidate()  # labelled LIVE
        with pytest.raises(AcquisitionError, match="acl_unverified"):
            world.candidate(cls=LiveAcquisition)  # fake transport, labelled MOCK
        assert staged_names(world) == []
        assert world.catalog.db.execute("SELECT COUNT(*) FROM live_candidate").fetchone()[0] == 0
    finally:
        restore(world.staging.root)
    attempt, _ = world.candidate()
    assert world.staging.view(LIVE_ALIAS)["attempt_id"] == attempt


LABELS = {
    "mock": b'{"mode":"MOCK"}',
    "lowercase": b'{"mode":"live"}',
    "padded": b'{"mode":"LIVE "}',
    "duplicate_key": b'{"mode":"LIVE","mode":"MOCK"}',
    "null": b'{"mode":null}',
    "missing": b'{"alias":"x"}',
}


@pytest.mark.parametrize("label", list(LABELS.values()), ids=list(LABELS))
def test_a_manifest_label_can_never_switch_the_storage_check_off(world, label):
    attempt = "attempt-00000000000000a1"
    widen(world.staging.root)
    try:
        with pytest.raises(AcquisitionError, match="acl_unverified"):
            world.staging.stage(attempt, LIVE_ALIAS, label, b"{}", lambda: True)
        assert staged_names(world) == []
        assert world.catalog.db.execute("SELECT COUNT(*) FROM live_candidate").fetchone()[0] == 0
    finally:
        restore(world.staging.root)
    world.staging.stage(attempt, LIVE_ALIAS, label, b"{}", lambda: True)  # verified storage accepts the same bytes
    assert world.staging.view(LIVE_ALIAS)["attempt_id"] == attempt


def test_promotion_also_requires_verified_generation_storage(world):
    attempt, digest = world.candidate()
    widen(world.gens.root)
    try:
        refused(world, attempt, digest, 503, "acl_unverified")
        nothing_written(world)
    finally:
        restore(world.gens.root)
    assert world.approve(attempt, digest)["state"] == "approved"


# -- S3: a new directory is verified after mkdir and before the first write -----------------------------------


@pytest.fixture
def writes(monkeypatch):
    """Count every private-file write; the real writer still runs."""
    seen = []
    real = files._write
    monkeypatch.setattr(files, "_write", lambda path, data: (seen.append(path), real(path, data))[1])
    return seen


def deny_new_directories(monkeypatch, suffix):
    """Fail closed for new directories ending in ``suffix`` only; roots and everything else use the real verifier."""
    real = files.verify_private_path

    def verify(path):
        if Path(path).name.endswith(suffix):
            raise AcquisitionError("acl_unverified")
        real(path)

    monkeypatch.setattr(files, "verify_private_path", verify)
    return real


def test_staging_verifies_the_new_candidate_directory_before_writing_any_byte(world, writes, monkeypatch):
    deny_new_directories(monkeypatch, ".tmp")
    with pytest.raises(AcquisitionError, match="acl_unverified"):
        world.candidate()
    assert writes == []  # the old order wrote both files first and only then noticed
    assert staged_names(world) == []
    states = {r[0] for r in world.catalog.db.execute("SELECT state FROM live_candidate")}
    assert states <= {"erased"}


def test_promotion_verifies_the_temporary_directory_before_writing_any_byte(world, writes, monkeypatch):
    attempt, digest = world.candidate()
    writes.clear()
    real = deny_new_directories(monkeypatch, ".obj")
    refused(world, attempt, digest, 503, "acl_unverified")
    assert [p for p in writes if p.parent.name.endswith(".obj")] == []  # the .check writes before it are legitimate
    nothing_written(world)
    monkeypatch.setattr(files, "verify_private_path", real)
    assert world.approve(attempt, digest)["state"] == "approved"  # the candidate was left intact and still approvable


def test_the_throwaway_ingestion_directory_is_verified_before_writing_any_byte(world, writes, monkeypatch):
    attempt, digest = world.candidate()
    writes.clear()
    deny_new_directories(monkeypatch, ".check")
    refused(world, attempt, digest, 503, "acl_unverified")
    assert writes == []
    nothing_written(world)


# -- the owner routes ----------------------------------------------------------------------------------------


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr("api.beta.LiveAcquisition", FixtureLive)


def status_of(client):
    return client.get(LIVE + "/status").json()


def test_owner_flow_refresh_review_approve_through_the_routes(harness, keys, fake, routed):
    fake.outcomes = {"DocumentReference:clinical-note": "59100"}
    with build(harness, keys, fake) as client:
        harness.login(client)
        body = refresh_and_wait(harness, client)
        candidate = body["candidate"]
        assert candidate["review"]["operations"]["DocumentReference:clinical-note"]["informational_codes"] == ["59100"]
        assert body["generation"] is None and (body["serving"], body["publication"]) == ("unavailable", "disabled")
        approve = f"{LIVE}/candidate/{candidate['attempt_id']}/approve"
        headers = {"X-Beta-Candidate-Hash": candidate["manifest_sha256"]}

        def post(path, **extra):
            return client.post(
                path,
                headers={"Origin": "https://beta.invalid", "X-CSRF-Token": harness.csrf, **extra},
            )

        assert post(approve).status_code == 400  # the reviewed hash is required
        assert client.post(approve, headers=headers).status_code == 403  # CSRF and Origin are still required
        assert post(approve, **headers).json()["error"]["code"] == "step_up_required"  # a login alone is not enough
        assert step_up(harness, client).status_code == 200
        assert post(approve, **{"X-Beta-Candidate-Hash": "f" * 64}).json()["error"]["code"] == "candidate_hash_mismatch"
        assert (
            post(approve, **headers).json()["error"]["code"] == "step_up_required"
        )  # the mismatch spent the elevation
        assert step_up(harness, client).status_code == 200
        result = post(approve, **headers)
        assert result.status_code == 201 and result.json()["state"] == "approved"
        after = status_of(client)
        assert after["candidate"] is None and after["generation"]["generation_id"] == result.json()["generation_id"]
        assert (after["serving"], after["publication"]) == ("unavailable", "disabled")  # nothing is served
        assert after["generation"]["review"]["completeness"] == "unconfirmed"
        assert post(approve, **headers).json()["error"]["code"] == "step_up_required"  # the approval spent it
        assert step_up(harness, client).status_code == 200
        assert post(approve, **headers).status_code == 404  # replay with a new elevation: the candidate is gone
        assert LIVE_PATIENT not in json.dumps(after) and BODY not in json.dumps(after)
        assert client.get(f"{LIVE}/generations/g/runs/r").status_code == 404  # no clinical access, no MOCK routes


def test_approval_needs_a_recent_login_and_no_refresh_in_flight(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        candidate = refresh_and_wait(harness, client)["candidate"]
        approve = f"{LIVE}/candidate/{candidate['attempt_id']}/approve"
        headers = {"X-Beta-Candidate-Hash": candidate["manifest_sha256"]}
        stale = harness.post(client, approve, **headers)  # signed in, but no step-up
        assert (stale.status_code, stale.json()["error"]["code"]) == (401, "step_up_required")
        harness.clock += 400  # past the refresh cooldown
        harness.login(client)  # a new session
        assert step_up(harness, client).status_code == 200
        fake.hold = True
        before = len(fake.calls)
        assert harness.post(client, LIVE + "/refresh", key="second-key-12345").status_code == 202
        until = time.monotonic() + 1
        while len(fake.calls) < before + 2 and time.monotonic() < until:
            time.sleep(0.01)
        busy = harness.post(client, approve, **headers)
        assert (busy.status_code, busy.json()["error"]["code"]) == (409, "refresh_busy")
        fake.hold = False
        assert harness.post(client, LIVE + "/candidate/erase").status_code == 204


def test_a_mock_alias_and_a_disabled_setup_have_no_approval_route(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        for path in (f"{ALPHA}/candidate/x/approve", f"{ALPHA}/generation/erase"):
            assert harness.post(client, path, **{"X-Beta-Candidate-Hash": "a" * 64}).status_code == 404
    with build(harness, keys, fake, live=False) as client:
        harness.login(client)
        assert (
            harness.post(client, f"{LIVE}/candidate/x/approve", **{"X-Beta-Candidate-Hash": "a" * 64}).status_code
            == 404
        )


def test_owner_erase_revocation_and_expiry_remove_the_generation_through_the_routes(harness, keys, fake, routed):
    def approve(client):
        candidate = refresh_and_wait(harness, client, key=f"key-{harness.clock}-padding")["candidate"]
        path = f"{LIVE}/candidate/{candidate['attempt_id']}/approve"
        assert step_up(harness, client).status_code == 200
        assert harness.post(client, path, **{"X-Beta-Candidate-Hash": candidate["manifest_sha256"]}).status_code == 201

    generations = harness.db.parent / "live-generations" / "objects"
    with build(harness, keys, fake) as client:
        harness.login(client)
        approve(client)
        assert len(list(generations.iterdir())) == 1
        assert harness.post(client, LIVE + "/generation/erase").status_code == 204
        assert list(generations.iterdir()) == [] and status_of(client)["generation"] is None
        harness.clock += 400
        harness.login(client)
        approve(client)
        assert harness.post(client, LIVE + "/revoke").status_code == 204
        assert list(generations.iterdir()) == [] and staged(harness) == []
    with build(harness, keys, fake):
        pass  # a revoked alias stays erased across a restart


def test_a_generation_survives_restart_and_expires_after_thirty_days(harness, keys, fake, routed):
    with build(harness, keys, fake) as client:
        harness.login(client)
        candidate = refresh_and_wait(harness, client)["candidate"]
        path = f"{LIVE}/candidate/{candidate['attempt_id']}/approve"
        assert step_up(harness, client).status_code == 200
        gid = harness.post(client, path, **{"X-Beta-Candidate-Hash": candidate["manifest_sha256"]}).json()[
            "generation_id"
        ]
    with build(harness, keys, fake) as client:
        harness.login(client)
        assert status_of(client)["generation"]["generation_id"] == gid
        harness.clock += GENERATION_RETENTION_S + 1
        harness.login(client)
        assert status_of(client)["generation"] is None
        assert list((harness.db.parent / "live-generations" / "objects").iterdir()) == []


def test_unverified_storage_fails_the_refresh_closed_at_the_route(harness, keys, fake):
    with build(harness, keys, fake) as client:  # fake transport => the candidate is labelled MOCK
        harness.login(client)
        widen(harness.db.parent / "live-staging")
        try:
            attempt = refresh_and_wait(harness, client)["attempt"]
            assert (attempt["state"], attempt["error_code"]) == ("failed", "acl_unverified")
            assert staged(harness) == []
        finally:
            restore(harness.db.parent / "live-staging")


def poll(condition, seconds=4.0):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_expired_data_is_purged_while_idle_without_any_request(harness, keys, fake, routed, monkeypatch):
    monkeypatch.setattr("api.beta.LIVE_PURGE_INTERVAL_S", 0.05)
    objects = harness.db.parent / "live-generations" / "objects"
    with build(harness, keys, fake) as client:
        harness.login(client)
        candidate = refresh_and_wait(harness, client)["candidate"]
        path = f"{LIVE}/candidate/{candidate['attempt_id']}/approve"
        assert step_up(harness, client).status_code == 200
        assert harness.post(client, path, **{"X-Beta-Candidate-Hash": candidate["manifest_sha256"]}).status_code == 201
        harness.clock += 400  # past the refresh cooldown
        harness.login(client)
        refresh_and_wait(harness, client, key="second-key-12345")
        assert len(staged(harness)) == 1 and len(list(objects.iterdir())) == 1
        # No request is made from here on: only the background pass can erase anything.
        harness.clock += STAGING_RETENTION_S + 1
        assert poll(lambda: staged(harness) == []), "expired candidate survived an idle process"
        assert len(list(objects.iterdir())) == 1  # the generation (30 days) is not yet expired
        harness.clock += GENERATION_RETENTION_S
        assert poll(lambda: list(objects.iterdir()) == []), "expired generation survived an idle process"


def test_a_failed_purge_pass_is_audited_and_does_not_stop_the_loop(harness, keys, fake, monkeypatch):
    monkeypatch.setattr("api.beta.LIVE_PURGE_INTERVAL_S", 0.05)
    real, state = LiveStaging.purge, {"armed": False, "raised": 0}

    def flaky(self):
        if state["armed"] and not state["raised"]:
            state["raised"] += 1
            raise RuntimeError("invented failure")
        return real(self)

    monkeypatch.setattr(LiveStaging, "purge", flaky)
    with build(harness, keys, fake) as client:
        harness.login(client)
        refresh_and_wait(harness, client)
        assert len(staged(harness)) == 1
        state["armed"] = True
        harness.clock += STAGING_RETENTION_S + 1
        assert poll(lambda: staged(harness) == []), "the loop stopped after a failed pass"
    assert state["raised"] == 1
    catalog = Catalog(harness.db, lambda: harness.clock)
    try:
        assert "live_purge_failed" in {r["code"] for r in catalog.db.execute("SELECT code FROM audit")}
    finally:
        catalog.close()
