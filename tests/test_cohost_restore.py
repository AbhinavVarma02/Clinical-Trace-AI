"""Release recovery contracts with no real HF/network/settings mutations."""

from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
import sys

import pytest

from deploy.huggingface import space_cohost


@pytest.fixture
def hf(monkeypatch):
    # The host administration SDK is optional at runtime; tests need no download.
    huggingface_hub = ModuleType("huggingface_hub")

    class CommitOperationAdd:
        def __init__(self, path_in_repo, path_or_fileobj):
            self.path_in_repo = path_in_repo
            self.contents = Path(path_or_fileobj).read_bytes()

    class CommitOperationDelete:
        def __init__(self, path_in_repo):
            self.path_in_repo = path_in_repo

    huggingface_hub.CommitOperationAdd = CommitOperationAdd
    huggingface_hub.CommitOperationDelete = CommitOperationDelete
    huggingface_hub.HfApi = None
    huggingface_hub.snapshot_download = None
    monkeypatch.setitem(sys.modules, "huggingface_hub", huggingface_hub)

    old = {
        "COHOST_MANIFEST.json",
        "start.sh",
        "api/v2.py",
        "dashboard/app.py",
        "deploy/huggingface/nginx.conf",
    }
    api = Mock()
    api.space_info.return_value.sha = "new-release"
    api.list_repo_files.side_effect = lambda *a, revision, **kw: sorted(
        old if revision == "known-good" else old | {"added.txt"}
    )
    api.create_commit.return_value.oid = "restored"

    def snapshot(*args, local_dir, **kwargs):
        for name in old:
            p = Path(local_dir) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("original " + name)
        (Path(local_dir) / "unrelated-cache.txt").write_text("do not upload")

    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: api)
    download = Mock(side_effect=snapshot)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    return api, old, download


def test_exact_cohost_restoration_is_one_parent_pinned_commit_without_settings_changes(hf):
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete

    api, old, _ = hf
    space_cohost.restore_cohost("known-good", "new-release")
    api.create_commit.assert_called_once()
    args = api.create_commit.call_args.kwargs
    assert args["parent_commit"] == "new-release"
    additions = [o for o in args["operations"] if isinstance(o, CommitOperationAdd)]
    deletions = [o for o in args["operations"] if isinstance(o, CommitOperationDelete)]
    assert {o.path_in_repo for o in additions} == old
    assert {o.path_in_repo for o in deletions} == {"added.txt"}
    assert all("cache" not in o.path_in_repo for o in args["operations"])
    assert not any("secret" in c[0] or "variable" in c[0] or "hardware" in c[0] for c in api.method_calls)


def test_moved_space_stops_before_download_or_write(hf):
    api, _, download = hf
    with pytest.raises(SystemExit, match="Space moved"):
        space_cohost.restore_cohost("known-good", "different-parent")
    download.assert_not_called()
    api.create_commit.assert_not_called()


def test_v1_only_tree_requires_explicit_other_rollback(hf):
    api, old, download = hf
    old.remove("COHOST_MANIFEST.json")
    with pytest.raises(SystemExit, match="not a complete co-host"):
        space_cohost.restore_cohost("known-good", "new-release")
    download.assert_not_called()
    api.create_commit.assert_not_called()


def test_atomic_parent_conflict_is_not_retried_against_a_new_release(hf):
    api, _, _ = hf
    api.create_commit.side_effect = RuntimeError("parent commit changed during upload")
    with pytest.raises(RuntimeError, match="parent commit changed"):
        space_cohost.restore_cohost("known-good", "new-release")
    api.create_commit.assert_called_once()
    assert api.create_commit.call_args.kwargs["parent_commit"] == "new-release"


@pytest.mark.parametrize("approved,parent", [(False, "new-release"), (True, None)])
def test_restore_cli_requires_owner_approval_and_explicit_current_parent(monkeypatch, approved, parent):
    args = ["space_cohost.py", "restore-cohost", "--revision", "known-good"]
    if approved:
        args.append("--i-have-owner-approval")
    if parent:
        args += ["--expected-current-revision", parent]
    monkeypatch.setattr("sys.argv", args)
    restore = Mock()
    monkeypatch.setattr(space_cohost, "restore_cohost", restore)
    with pytest.raises(SystemExit):
        space_cohost.main()
    restore.assert_not_called()
