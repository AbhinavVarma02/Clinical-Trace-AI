"""Publication copy and its offline export launcher. Every byte is invented by test fixtures; nothing reads Epic or
private data, and nothing is published."""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import pytest

from api.beta_publication import FORMAT, publication_document
from scripts import run_owner_epic_export as export
from tests.test_beta_live_generations import keys as keys_fixture, world as world_fixture
from tests.test_beta_live_record import approved, read

keys = keys_fixture
world = world_fixture


def test_copy_withholds_attempt_identity_and_labels_a_never_refreshed_snapshot(world):
    approved(world)
    view = read(world)
    original = copy.deepcopy(view)
    document = publication_document(view, exported_at=1_800_000_000.0)
    assert view == original  # the accepted projection is never modified
    assert document["format"] == FORMAT and document["withheld"] == ["attempt_id"]
    published = document["view"]
    assert "attempt_id" not in published["provenance"] and "attempt_id" in view["provenance"]
    assert published["provenance"] == {key: value for key, value in view["provenance"].items() if key != "attempt_id"}
    assert (published["serving"], published["freshness"], published["publication"]) == (
        "stale",
        "stale",
        "published_snapshot",
    )
    # Nothing new is read: the record, coverage, attempt summary and active generation are the accepted projection.
    for key in ("record", "coverage", "attempt", "active", "alias", "source", "data_origin", "completeness", "mode"):
        assert published[key] == view[key]
    codes = [warning["code"] for warning in published["warnings"]]
    assert codes[:2] == ["published_snapshot", "clinical_ai_disabled"]
    assert "synthetic_live_fixture" in codes and "completeness_unconfirmed" in codes
    assert "live_access_disabled" not in codes and not any(code.startswith("serving_") for code in codes)
    assert view["provenance"]["attempt_id"] not in json.dumps(document)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(mode="MOCK"),
        lambda v: v.update(data_origin="epic_sandbox"),  # synthetic transport cannot be relabelled as Epic data
        lambda v: v.update(source="epic-sandbox-mock"),
        lambda v: v.update(completeness="complete"),
        lambda v: v.update(evidence_error="generation_integrity"),
        lambda v: v.update(record=None),
        lambda v: v["provenance"].update(generation_id="f" * 32),
        lambda v: v["provenance"].update(synthetic_transport=False),
        lambda v: v.update(label="Approved LIVE patient"),  # every synthetic marker must agree
        lambda v: v.update(warnings=[w for w in v["warnings"] if w["code"] != "synthetic_live_fixture"]),
        lambda v: [item["source"].update(synthetic=False) for item in v["record"]["notes"]["items"]],
    ],
)
def test_only_a_verified_consistently_labelled_live_view_can_become_a_copy(world, change):
    approved(world)
    view = read(world)
    change(view)
    with pytest.raises(ValueError):
        publication_document(view, exported_at=0.0)


def _arguments(tmp_path, **overrides):
    """Invented roots under tmp_path and an existing output folder; nothing is read or written by the launcher."""
    out = tmp_path / "publication"
    out.mkdir(exist_ok=True)
    values = {
        "--binding-root": str(tmp_path / "binding"),
        "--binding-sha256": "d" * 64,
        "--private-root": str(tmp_path / "epic-private"),
        "--alias": "invented-alpha",
        "--patient-id": "invented-live-patient",
        "--attempt-id": "a" * 32,
        "--generation-id": "b" * 32,
        "--manifest-sha256": "c" * 64,
        "--bundle-sha256": "d" * 64,
        "--out": str(out / "record.json"),
    }
    values.update(overrides)
    argv = ["--accept-owner-policy"]
    for name, value in values.items():
        if value is not None:
            argv += [name, value]
    return argv


def _link_directory(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_export_is_disabled_without_explicit_owner_policy(capsys):
    assert export.main([]) == 2
    assert "disabled" in capsys.readouterr().out


def test_complete_arguments_reach_only_the_private_read_boundary(tmp_path, monkeypatch):
    reached = []
    monkeypatch.setattr(export, "_read_view", lambda args: reached.append(args) or (_ for _ in ()).throw(RuntimeError))
    assert export.main(_arguments(tmp_path)) == 1
    assert len(reached) == 1 and reached[0].generation_id == "b" * 32
    assert reached[0].out == (tmp_path / "publication").resolve() / "record.json"


@pytest.mark.parametrize(
    "overrides",
    [
        {"--attempt-id": None},
        {"--generation-id": "b" * 31},
        {"--manifest-sha256": "C" * 64},
        {"--bundle-sha256": None},
        {"--binding-sha256": "x" * 64},
        {"--alias": "Invented"},
        {"--patient-id": ".."},
        {"--private-root": "relative-private"},
        {"--out": "relative.json"},
        {"--unexpected": "value"},
    ],
)
def test_incomplete_arguments_never_reach_private_data(tmp_path, overrides, monkeypatch):
    monkeypatch.setattr(export, "_read_view", lambda args: pytest.fail("private data reached"))
    argv = _arguments(tmp_path, **{key: value for key, value in overrides.items() if key != "--unexpected"})
    if "--unexpected" in overrides:
        argv += ["--unexpected", overrides["--unexpected"]]
    assert export.main(argv) == 1


def _misplaced(tmp_path: Path, case: str, monkeypatch) -> dict:
    repo = Path(__file__).resolve().parents[1]
    if case == "junction-into-repository":
        # A stand-in repository inside tmp_path, so the link never points at the real checkout.
        stand_in = tmp_path / "stand-in-repo"
        stand_in.mkdir()
        monkeypatch.setattr(export, "_REPO", stand_in)
        _link_directory(stand_in, tmp_path / "looks-outside")
        return {"--out": str(tmp_path / "looks-outside" / "record.json")}
    if case == "txt":
        return {"--out": str(tmp_path / "publication" / "record.txt")}
    if case == "missing-parent":
        return {"--out": str(tmp_path / "absent" / "record.json")}
    if case == "dot-dot":
        return {"--out": str(tmp_path / "publication" / ".." / "publication" / "record.json")}
    if case == "repository":
        return {"--out": str(repo / "record.json")}
    if case == "inside-private-root":
        (tmp_path / "epic-private").mkdir()
        return {"--out": str(tmp_path / "epic-private" / "record.json")}
    if case == "parent-holds-private-root":
        return {"--out": str(tmp_path / "record.json")}
    if case == "git-work-tree":
        (tmp_path / "tree" / ".git").mkdir(parents=True)
        (tmp_path / "tree" / "copy").mkdir()
        return {"--out": str(tmp_path / "tree" / "copy" / "record.json")}
    if case == "overlapping-roots":
        return {"--binding-root": str(tmp_path / "epic-private" / "binding")}
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "txt",
        "missing-parent",
        "dot-dot",
        "repository",
        "junction-into-repository",
        "inside-private-root",
        "parent-holds-private-root",
        "git-work-tree",
        "overlapping-roots",
    ],
)
def test_misplaced_output_or_roots_never_reach_private_data(tmp_path, case, monkeypatch):
    monkeypatch.setattr(export, "_read_view", lambda args: pytest.fail("private data reached"))
    try:
        assert export.main(_arguments(tmp_path, **_misplaced(tmp_path, case, monkeypatch))) == 1
    finally:
        link = tmp_path / "looks-outside"
        if link.is_symlink() or link.is_junction():
            link.unlink() if link.is_symlink() else link.rmdir()  # removes the link only, never its target


def test_an_existing_output_file_is_never_overwritten(tmp_path, monkeypatch):
    target = tmp_path / "publication" / "record.json"
    target.parent.mkdir()
    target.write_text("{}")
    monkeypatch.setattr(export, "_read_view", lambda args: pytest.fail("private data reached"))
    assert export.main(_arguments(tmp_path)) == 1
    assert target.read_text() == "{}"


@pytest.mark.skipif(sys.platform != "win32", reason="the attended owner console is Windows-only")
def test_the_read_locks_the_environment_and_requires_an_attended_owner_console(tmp_path, monkeypatch):
    pytest.importorskip("fido2")
    from api import beta_owner_console, beta_owner_operator
    from scripts import run_owner_https_diagnostic

    order = []
    monkeypatch.setattr(run_owner_https_diagnostic, "_lock_environment", lambda: order.append("lock"))
    monkeypatch.setattr(beta_owner_console, "_local_console", lambda: order.append("console") or False)
    monkeypatch.setattr(beta_owner_operator, "open_owner_binding", lambda *a, **k: pytest.fail("owner binding opened"))
    assert export.main(_arguments(tmp_path)) == 1
    assert order == ["lock", "console"]
    assert not (tmp_path / "publication" / "record.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS custody of the private root is Windows-only")
def test_the_export_reads_a_promoted_generation_end_to_end_into_one_private_copy(tmp_path, monkeypatch, keys):
    """The real read path, on an invented generation laid out like a private Epic root. Only the owner binding, the
    attended console and the process lock are stubbed; custody, catalog, generations and the binding pins are real."""
    pytest.importorskip("fido2")
    from types import SimpleNamespace

    from api import beta_owner_console, beta_owner_operator
    from scripts import run_owner_https_diagnostic
    from src.interop.epic import beta_live_staging as files
    from src.interop.epic import settings
    from src.interop.epic.beta_catalog import Catalog
    from src.interop.epic.beta_live_generations import LiveGenerations
    from src.interop.epic.beta_live_staging import LiveAllowlist, LiveStaging
    from tests.test_beta_live_generations import Clock, World
    from tests.test_beta_live_route import LIVE_ALIAS, LIVE_PATIENT, FakeEpic

    class RealLayoutWorld(World):
        """TEST ONLY: invented evidence labelled non-synthetic, in the real private-root layout, promoted with the
        real-promotion switch that only this test turns on."""

        def __init__(self, root, key, epic):
            self.root, self.key, self.epic, self.n = root, key, epic, 0
            self.clock = Clock()
            self.clock.now = time.time()  # the export reads with real time and refuses evidence from the future
            self.catalog = Catalog(root / "catalog.sqlite", self.clock)
            self.allow = LiveAllowlist({LIVE_ALIAS: LIVE_PATIENT})
            self.staging = LiveStaging(self.catalog, root / "staging")
            self.hooks, self.ok, self.fresh = [], True, True
            self.gens = LiveGenerations(
                self.catalog, root / "generations", self.allow, self.staging, real_promotion=True, hook=self.hook
            )

    monkeypatch.setenv(settings.ENABLE_ENV, "1")
    private_root = tmp_path / "epic-private"
    files.make_private_directory(private_root)
    world = RealLayoutWorld(private_root, keys[1], FakeEpic())
    try:
        attempt, digest = world.candidate(mutate=lambda manifest: manifest.update(synthetic_transport=False))
        generation = world.approve(attempt, digest)["generation_id"]
        row = world.rows()[generation]
    finally:
        world.catalog.close()
    manifest = json.loads((private_root / "generations" / "objects" / generation / "manifest.json").read_bytes())
    out_dir = tmp_path / "publication"
    files.make_private_directory(out_dir)

    owner = SimpleNamespace(load=lambda: SimpleNamespace(credential=object(), revoked=False), close=lambda: None)
    monkeypatch.setattr(run_owner_https_diagnostic, "_lock_environment", lambda: None)
    monkeypatch.setattr(beta_owner_console, "_local_console", lambda: True)
    monkeypatch.setattr(beta_owner_operator, "open_owner_binding", lambda *args, **kwargs: owner)
    argv = _arguments(
        tmp_path,
        **{
            "--private-root": str(private_root),
            "--alias": LIVE_ALIAS,
            "--patient-id": LIVE_PATIENT,
            "--attempt-id": attempt,
            "--generation-id": generation,
            "--manifest-sha256": row["manifest_sha256"],
            "--bundle-sha256": manifest["bundle_sha256"],
        },
    )
    assert export.main(argv) == 0
    document = json.loads((out_dir / "record.json").read_text(encoding="utf-8"))
    view = document["view"]
    assert document["format"] == FORMAT and document["withheld"] == ["attempt_id"]
    assert (view["data_origin"], view["label"], view["publication"]) == (
        "epic_sandbox",
        "Approved LIVE patient",
        "published_snapshot",
    )
    assert view["provenance"]["generation_id"] == generation and "attempt_id" not in view["provenance"]
    assert view["provenance"]["bundle_sha256"] == manifest["bundle_sha256"]
    assert attempt not in json.dumps(document)
    assert export.main(argv) == 1  # never overwritten

    wrong = list(argv)
    wrong[wrong.index("--bundle-sha256") + 1] = "0" * 64
    (out_dir / "record.json").unlink()
    assert export.main(wrong) == 1  # a binding that does not match the promoted generation reads nothing
    assert not (out_dir / "record.json").exists()
