"""Stage, verify, publish or roll back the co-hosted Hugging Face Space (v1 dashboard + 2.0 API).

The existing public Space must stay unchanged unless the owner explicitly approves publication.
``stage`` and ``verify`` never touch the Space; ``publish`` and ``rollback`` refuse to run without
``--i-have-owner-approval`` and pin the expected parent commit so concurrent edits are not lost.

    python deploy/huggingface/space_cohost.py stage  --out <dir>
    python deploy/huggingface/space_cohost.py verify --out <dir>             # docker build + checks
    python deploy/huggingface/space_cohost.py publish --out <dir> --i-have-owner-approval
    python deploy/huggingface/space_cohost.py rollback --i-have-owner-approval  # restore 91e7685
    python deploy/huggingface/space_cohost.py restore-cohost --revision <known-good-cohost> \
        --expected-current-revision <failed-publication> --i-have-owner-approval

Staging = the Space tree at the pinned revision (keeps its v1 dashboard fork, README and LFS
models) + the 2.0 backend/frontend sources from ``git HEAD`` + the co-host Dockerfile, nginx config
and entrypoint. Secrets are never written to the tree; ``publish`` sets them as Space secrets.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SPACE = "abhinavvathadi/clinical-trace-ai"
V1_REVISION = "91e768574cdb7871dbc18a1d919feecab7f92830"
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OVERLAY = [
    "api",
    "src",
    "prompts",
    "data/fhir",
    "evaluation",
    "frontend",
    "requirements.txt",
    "requirements-constraints.txt",
    "deploy/huggingface/nginx.conf",
]
KEEP_FROM_SPACE = ["dashboard", "models", "README.md", ".gitattributes", ".gitignore", ".env.example"]


def stage(out: Path, revision: str) -> dict:
    from huggingface_hub import snapshot_download

    if out.exists():
        shutil.rmtree(out)
    snapshot_download(SPACE, repo_type="space", revision=revision, local_dir=str(out))
    shutil.rmtree(out / ".cache", ignore_errors=True)
    for name in OVERLAY:
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", *OVERLAY], cwd=ROOT, capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(out, filter="data")
    shutil.copyfile(HERE / "Dockerfile.cohost", out / "Dockerfile")
    # Shell scripts must keep LF endings regardless of the checkout platform.
    (out / "start.sh").write_bytes((HERE / "start.sh").read_bytes().replace(b"\r\n", b"\n"))
    (out / ".dockerignore").write_text(
        "frontend/node_modules/\nfrontend/dist/\nfrontend/dist-workspace/\nvar/\n**/__pycache__/\n", encoding="utf-8"
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    manifest = {
        "space": SPACE,
        "parent_revision": revision,
        "source_commit": commit,
        "overlay": OVERLAY,
        "kept_from_space": KEEP_FROM_SPACE,
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out / "COHOST_MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=1))
    return manifest


def _get(url: str, headers: dict | None = None, data: bytes | None = None, timeout: float = 120):
    request = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("content-type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("content-type", ""), exc.read()


def verify(out: Path, port: int = 7861) -> bool:
    """Build the staged tree and check v1 and 2.0 behaviour through nginx exactly as the Space would."""
    tag = "clinical-trace-ai:cohost-verify"
    subprocess.run(["docker", "build", "-t", tag, str(out)], check=True)
    edge_secret = secrets.token_urlsafe(24)
    name = f"ct-cohost-{secrets.token_hex(3)}"
    started = time.perf_counter()
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--cpus=2",
            "--memory=16g",
            "-p",
            f"{port}:7860",
            "-e",
            f"CLINICAL_TRACE_EDGE_SECRET={edge_secret}",
            tag,
        ],
        check=True,
        capture_output=True,
    )
    base = f"http://127.0.0.1:{port}"
    checks: list[dict] = []

    def check(label: str, ok: bool, detail: object = None) -> None:
        checks.append({"check": label, "ok": bool(ok), **({"detail": detail} if detail is not None else {})})

    try:
        ready = None
        while time.perf_counter() - started < 600:
            try:
                if _get(base + "/api/v2/system/status", timeout=5)[0] == 200:
                    ready = time.perf_counter() - started
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(1)
        check("2.0 API ready through nginx", ready is not None, {"seconds": round(ready or -1, 1)})
        streamlit = None
        while time.perf_counter() - started < 600:
            try:
                status, _, body = _get(base + "/_stcore/health", timeout=5)
                if status == 200:
                    streamlit = body.decode()
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(1)
        check("v1 Streamlit health through nginx", streamlit == "ok", streamlit)
        status, ctype, body = _get(base + "/")
        check("v1 dashboard page served at /", status == 200 and b"<html" in body.lower(), status)
        logs = subprocess.run(
            [
                "docker",
                "exec",
                name,
                "python",
                "-c",
                "import urllib.request,json;"
                "print(*[urllib.request.urlopen('http://127.0.0.1:8000' + p).status"
                " for p in ('/health', '/model-info')])",
            ],
            capture_output=True,
            text=True,
        )
        check(
            "v1 API (/health, /model-info) on loopback for the dashboard",
            logs.stdout.strip() == "200 200",
            logs.stdout.strip() or logs.stderr[-200:],
        )
        ws = secrets.token_hex(16)
        public = {"X-Demo-Persona": "clinician.demo", "X-Demo-Workspace": ws, "Content-Type": "application/json"}
        status, _, _ = _get(base + "/api/v2/patients", public)
        check("2.0 API refuses requests without the edge secret", status == 403, status)
        edge = {**public, "X-Edge-Secret": edge_secret, "X-Edge-Client": "ab" * 12}
        status, _, _ = _get(base + "/api/v2/patients", {k: v for k, v in edge.items() if k != "X-Demo-Workspace"})
        check("public mode requires a workspace", status == 400, status)
        body = json.dumps({"patient_id": "synthetic_p001", "question": "Summarize this patient journey"}).encode()
        t0 = time.perf_counter()
        status, ctype, raw = _get(base + "/api/v2/agent/runs/stream", edge, body)
        frames = raw.decode().split("\n\n")
        result = next((f for f in frames if f.startswith("event: result")), None)
        run = json.loads(result.split("\ndata: ", 1)[1]) if result else {}
        check(
            "streamed agent run through nginx",
            status == 200
            and "text/event-stream" in ctype
            and run.get("outcome") == "answered"
            and any(f.startswith("event: progress") for f in frames),
            {"ms": round((time.perf_counter() - t0) * 1000), "tools": len(run.get("tool_calls", []))},
        )
        status, _, raw = _get(base + "/api/v2/reviews", edge, json.dumps({"run_id": run.get("run_id")}).encode())
        review = json.loads(raw) if status == 200 else {}
        reviewer = {**edge, "X-Demo-Persona": "reviewer.demo"}
        status, _, raw = _get(
            base + f"/api/v2/reviews/{review.get('review_id')}/decision",
            reviewer,
            json.dumps({"decision": "approved", "artifact_hash": review.get("artifact_hash")}).encode(),
        )
        check(
            "review approved by a second persona",
            status == 200 and json.loads(raw).get("approval_valid") is True,
            status,
        )
        status, _, raw = _get(base + "/api/v2/patients/synthetic_p001/risk", edge)
        check("readmission risk with model loaded", status == 200 and json.loads(raw).get("available") is True, status)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    passed = all(c["ok"] for c in checks)
    print(json.dumps({"passed": passed, "checks": checks}, indent=1))
    return passed


def _require_approval(args: argparse.Namespace) -> None:
    if not args.i_have_owner_approval:
        sys.exit("Refusing: publishing or rolling back the public Space requires explicit owner approval.")


def publish(out: Path, edge_secret: str) -> None:
    """Publish the staged tree. Inference uses the edge relay: Spaces cannot reach Cloudflare, so the
    Space holds no inference URL or token; only the edge secret that authenticates edge traffic."""
    from huggingface_hub import HfApi

    api = HfApi()
    manifest = json.loads((out / "COHOST_MANIFEST.json").read_text(encoding="utf-8"))
    current = api.space_info(SPACE).sha
    if current != manifest["parent_revision"]:
        sys.exit(f"Space moved from {manifest['parent_revision']} to {current}; re-stage before publishing.")
    api.add_space_secret(SPACE, "CLINICAL_TRACE_EDGE_SECRET", edge_secret)
    for key, value in {
        "CLINICAL_TRACE_LLM_PROVIDER": "cloudflare-workers-ai-relay",
        "CLINICAL_TRACE_LLM_MODEL": "@cf/qwen/qwen3-30b-a3b-fp8",
    }.items():
        api.add_space_variable(SPACE, key, value)
    info = api.upload_folder(
        repo_id=SPACE,
        repo_type="space",
        folder_path=str(out),
        parent_commit=current,
        commit_message=f"Co-host Clinical-Trace AI 2.0 API beside the v1 dashboard ({manifest['source_commit'][:7]})",
        ignore_patterns=[".cache/**"],
    )
    print(f"published {info.oid}; rollback target {current}")


def rollback(revision: str) -> None:
    """Recommit the exact v1 tree: re-upload its files and delete anything added since."""
    from huggingface_hub import CommitOperationDelete, HfApi, snapshot_download

    api = HfApi()
    current = api.space_info(SPACE).sha
    old = set(api.list_repo_files(SPACE, repo_type="space", revision=revision))
    now = set(api.list_repo_files(SPACE, repo_type="space", revision=current))
    work = Path(os.environ.get("TEMP", "/tmp")) / f"ct-space-rollback-{revision[:7]}"
    snapshot_download(SPACE, repo_type="space", revision=revision, local_dir=str(work))
    api.upload_folder(
        repo_id=SPACE,
        repo_type="space",
        folder_path=str(work),
        parent_commit=current,
        ignore_patterns=[".cache/**"],
        commit_message=f"Roll back to v1 tree {revision[:7]}",
    )
    removed = sorted(now - old)
    if removed:
        api.create_commit(
            SPACE,
            repo_type="space",
            operations=[CommitOperationDelete(p) for p in removed],
            commit_message="Remove files added after the v1 tree",
            parent_commit=api.space_info(SPACE).sha,
        )
    for key in ("CLINICAL_TRACE_EDGE_SECRET", "CLINICAL_TRACE_LLM_API_KEY"):
        try:
            api.delete_space_secret(SPACE, key)
        except Exception:  # noqa: BLE001 - absent secrets are fine during rollback
            pass
    for key in ("CLINICAL_TRACE_LLM_PROVIDER", "CLINICAL_TRACE_LLM_BASE_URL", "CLINICAL_TRACE_LLM_MODEL"):
        try:
            api.delete_space_variable(SPACE, key)
        except Exception:  # noqa: BLE001
            pass
    print(f"restored tree of {revision}; removed {len(removed)} added files")


def restore_cohost(revision: str, expected_current: str) -> None:
    """Restore one exact co-host tree atomically, preserving current relay settings/secrets.

    The expected current parent prevents overwriting a concurrent Space release.
    This does not recover ephemeral SQLite data or change account hardware/billing.
    """
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi, snapshot_download

    api = HfApi()
    if api.space_info(SPACE).sha != expected_current:
        sys.exit("Space moved from the expected release; inspect it before restoring.")
    old = set(api.list_repo_files(SPACE, repo_type="space", revision=revision))
    required = {"COHOST_MANIFEST.json", "start.sh", "api/v2.py", "dashboard/app.py", "deploy/huggingface/nginx.conf"}
    if not required.issubset(old):
        sys.exit("Selected revision is not a complete co-host tree; use the v1-only rollback explicitly.")
    now = set(api.list_repo_files(SPACE, repo_type="space", revision=expected_current))
    with tempfile.TemporaryDirectory(prefix="ct-cohost-restore-") as scratch:
        work = Path(scratch)
        snapshot_download(SPACE, repo_type="space", revision=revision, local_dir=str(work))
        # Include only actual repository files, never snapshot metadata or cached extras.
        for name in old:
            if not (work / name).resolve().is_relative_to(work.resolve()) or not (work / name).is_file():
                sys.exit("Restoration snapshot contains a missing or invalid file.")
        operations = [CommitOperationAdd(path_in_repo=name, path_or_fileobj=work / name) for name in sorted(old)]
        operations.extend(CommitOperationDelete(name) for name in sorted(now - old))
        info = api.create_commit(
            SPACE,
            repo_type="space",
            operations=operations,
            parent_commit=expected_current,
            commit_message=f"Restore co-host tree {revision[:7]} without changing relay settings",
        )
    print(f"restored co-host tree {revision} at {info.oid}; settings/secrets unchanged")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["stage", "verify", "publish", "rollback", "restore-cohost"])
    parser.add_argument("--out", type=Path, default=Path(os.environ.get("TEMP", "/tmp")) / "ct-space-cohost")
    parser.add_argument("--revision", default=V1_REVISION)
    parser.add_argument("--secrets", type=Path, default=ROOT / "deploy" / "cloudflare" / ".prod-secrets.json")
    parser.add_argument("--i-have-owner-approval", action="store_true")
    parser.add_argument("--expected-current-revision", help="Required parent pin for restore-cohost")
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.out, args.revision)
    elif args.command == "verify":
        return 0 if verify(args.out) else 1
    elif args.command == "publish":
        _require_approval(args)
        values = json.loads(args.secrets.read_text(encoding="utf-8"))
        publish(args.out, values["EDGE_BACKEND_SECRET"])
    elif args.command == "restore-cohost":
        _require_approval(args)
        if not args.expected_current_revision:
            parser.error("restore-cohost requires --expected-current-revision")
        restore_cohost(args.revision, args.expected_current_revision)
    else:
        _require_approval(args)
        rollback(args.revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
