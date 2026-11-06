"""Epic adapter diagnostics and command line (MOCK/offline: mocked transports and fixtures only).

Spans, counters, reports and default CLI output must hold no token, key, patient
identifier, name or clinical text. LIVE commands must refuse to run without the
explicit opt-in flag.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src import observability
from src.interop.epic import cli, keys, settings
from src.interop.epic.auth import EpicTokenProvider
from src.interop.epic.client import EpicSandboxClient
from src.interop.epic.importer import fetch_patient
from src.interop.epic.workspace import write_snapshot
from tests.epic_support import FIXTURE, RETRIEVED_AT, fake_epic_transport, fixture_patient

TOKEN_VALUE = "unit-test-access-token-not-real"
ALLOWED_SPAN_ATTRIBUTES = set(observability.ALLOWED_ATTRIBUTES)


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(settings, "REPO_ROOT", repo)
    monkeypatch.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    monkeypatch.delenv(settings.ENABLE_ENV, raising=False)
    return settings.workspace_root()


def test_connector_spans_and_counters_hold_no_tokens_identifiers_or_text(ws_root, tmp_path):
    pid, resources, binaries = fixture_patient()
    token_transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            200, json={"access_token": TOKEN_VALUE, "token_type": "bearer", "expires_in": 3600, "scope": "Patient.read"}
        )
    )
    spans_file = tmp_path / "spans.jsonl"
    observability.configure(spans_file)
    try:
        tokens = EpicTokenProvider(
            "0000aaaa-test-client-id-not-real", keys.generate_private_key(2048), transport=token_transport
        )
        with EpicSandboxClient(tokens, transport=fake_epic_transport(resources, binaries)) as client:
            fetched = fetch_patient(client, pid)
        snapshot = write_snapshot(
            pid,
            fetched.raw_resources,
            fetched.binaries,
            retrieved_at=RETRIEVED_AT,
            mode="MOCK",
            fetch_summary=fetched.summary,
            snapshot_id="telemetry-0001",
        )
    finally:
        observability.shutdown()
    spans = [json.loads(line) for line in spans_file.read_text(encoding="utf-8").splitlines()]
    epic_spans = [s for s in spans if s["name"].startswith("clinical.epic.")]
    names = {s["name"] for s in epic_spans}
    assert {
        "clinical.epic.token",
        "clinical.epic.read.Patient",
        "clinical.epic.search.Observation",
        "clinical.epic.read.Binary",
    } <= names
    assert all(set(s["attributes"]) <= ALLOWED_SPAN_ATTRIBUTES for s in epic_spans)
    rendered = "\n".join(
        [
            spans_file.read_text(encoding="utf-8"),
            json.dumps(tokens.telemetry.describe()),
            json.dumps(fetched.summary),
            json.dumps(snapshot.manifest["mapping"]),
        ]
    )
    for forbidden in (TOKEN_VALUE, pid, "Fixture Alpha", "pneumonia", "Assessment", "0000aaaa-test-client"):
        assert forbidden not in rendered
    counters = tokens.telemetry.describe()["counters"]
    assert counters["token.ok"] == 1 and counters["read.Binary.ok"] == 1


def test_live_commands_refuse_to_run_without_the_opt_in_flag(ws_root, capsys):
    for argv in (["live", "token-check"], ["live", "smoke", "--patient", "x"], ["live", "import", "--patient", "x"]):
        assert cli.main(argv) == 2
        assert "disabled" in json.loads(capsys.readouterr().out)["error"]


def test_workspace_commands_build_verify_and_review_a_mock_snapshot(ws_root, capsys):
    assert cli.main(["workspace", "import-fixture", str(FIXTURE), "--key", "alpha"]) == 0
    created = json.loads(capsys.readouterr().out)
    snapshot_id = created["snapshot_id"]
    assert created["mode"] == "MOCK" and created["fetch_complete"] is True
    assert cli.main(["workspace", "list"]) == 0
    assert [s["snapshot_id"] for s in json.loads(capsys.readouterr().out)["snapshots"]] == [snapshot_id]
    assert cli.main(["workspace", "verify", snapshot_id]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.main(["workspace", "review", snapshot_id, "--question", "Summarize this patient's hospital course"]) == 0
    output = capsys.readouterr().out
    review = json.loads(output)
    assert review["label"] == "MOCK" and review["outcome"] == "answered"
    assert review["citation_provenance"]["resolved_to_epic_source"] == review["citation_provenance"]["citations"]
    assert "answer" not in review and "pneumonia" not in output.lower() and "Fixture Alpha" not in output

    assert cli.main(["workspace", "review", snapshot_id, "--question", "Summarize the course", "--show-answer"]) == 0
    assert "answer" in json.loads(capsys.readouterr().out)
