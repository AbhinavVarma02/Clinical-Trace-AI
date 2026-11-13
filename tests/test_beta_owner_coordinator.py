"""OFFLINE synthetic coordinator acceptance: invented identity/OS custody and ephemeral software proofs.

The actual adapter, mapper, private staging, promotion and projection run against FakeEpic. A labelled
test-only substitution accepts the existing synthetic_transport=True marker for positive coordination
tests; no fixture is restamped as provider evidence. The unchanged genuine gate refuses that fixture.
No owner SID/Hello/credentials/certificate/listener/network/provider evidence is established here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from api import beta_owner_coordinator as coordinator
from api.beta_live_record import LiveEvidenceBinding, live_record_view
from api.beta_owner_coordinator import AcquisitionPlan, GenuineOwnerCoordinatorApp
from src.interop.epic import beta_live_staging as files
from src.interop.epic import settings
from src.interop.epic.beta_acquisition import AcquisitionError
from src.interop.epic.beta_catalog import LIVE_POLICY_VERSION, Catalog, RefreshRejected
from src.interop.epic.beta_live_acquisition import LIVE_OPERATIONS, LiveRefresh
from src.interop.epic.beta_live_generations import LiveGenerations
from src.interop.epic.beta_staging import encoded
from src.interop.epic.mapping import content_sha256
from tests.test_beta_live_generations import FixtureLive, World
from tests.test_beta_live_route import LIVE_ALIAS, LIVE_PATIENT, FakeEpic
from tests.test_beta_owner_genuine import enroll, http, owned as owned_fixture
from tests.test_beta_owner_refresh import keys as keys_fixture
from tests.test_beta_synthetic_ui import _MemoryLoop

owned = owned_fixture
keys = keys_fixture
REAL_GATE = coordinator._require_genuine


@pytest.fixture
def prepared(owned, tmp_path, monkeypatch, keys):
    base, store, signer = owned
    enroll(base, signer, monkeypatch)
    monkeypatch.setenv(settings.ENABLE_ENV, "1")
    # Deliberately fake ACL boundary; real ACL/SID calls are forbidden by the subprocess guard.
    monkeypatch.setattr(files, "acl_problem", lambda path: None)
    epic = FakeEpic()
    world = World(tmp_path, keys[1], epic)
    world.gens.real_promotion = True  # TEST fixture's explicit promotion gate; not production configuration
    plan = AcquisitionPlan("n" * 43, LIVE_ALIAS, LIVE_PATIENT, tmp_path)
    live = LiveRefresh(
        keys[1],
        settings.EpicConfig("invented-client", Path("never-read.key"), "https://keys.invalid/jwks"),
        world.allow,
    )

    class OfflineAdapter(FixtureLive):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(epic.respond))

    monkeypatch.setattr(coordinator, "LiveAcquisition", OfflineAdapter)
    app = GenuineOwnerCoordinatorApp(
        store,
        plan,
        live,
        world.gens,
        enabled=True,
        policy_accepted=True,
        acquisition_approved=True,
    )
    app._auth.clock = world.clock
    yield app, world, signer
    world.catalog.close()


def synthetic_only(monkeypatch):
    def accept_labelled_fixture(manifest):
        assert manifest["synthetic_transport"] is True, "fixture must never masquerade as provider evidence"

    monkeypatch.setattr(coordinator, "_require_genuine", accept_labelled_fixture)


def action(app, name, body=None):
    return asyncio.run(app._dispatch_action(name, {} if body is None else body), loop_factory=_MemoryLoop)


def signed(app, signer, operation, body=None):
    options = action(app, operation + "/options", body)
    return {"flow_id": options["flow_id"], "credential": signer.proof(options)}


def finish(app, signer, operation, body=None):
    return action(app, operation + "/finish", signed(app, signer, operation, body))


def refuses(call, code):
    with pytest.raises(RefreshRejected) as error:
        call()
    assert error.value.code == code


def acquired(prepared, monkeypatch):
    synthetic_only(monkeypatch)
    acquisition, world, signer = prepared
    result = finish(acquisition, signer, "acquire")
    app = GenuineOwnerCoordinatorApp(
        acquisition._store,
        acquisition.plan,
        None,
        world.gens,
        enabled=True,
        policy_accepted=True,
        promotion_approved=True,
        approved_candidate=LiveEvidenceBinding(**result["result"]["provenance"]),
    )
    app._auth.clock = world.clock
    return app, world, signer, result


def test_defaults_and_scopes_deny_before_path_or_native_access(prepared):
    app, world, _ = prepared
    with pytest.raises(ValueError):
        GenuineOwnerCoordinatorApp(None, None, None, None)
    with pytest.raises(ValueError):
        coordinator.prepare_coordinator_https(app, Path("never-read.crt"), Path("never-read.key"))
    app.acquisition_approved = False
    refuses(lambda: action(app, "acquire/options"), "acquisition_not_approved")
    assert world.epic.calls == [] and not app.plan.guard.exists()
    with pytest.raises(ValueError):
        AcquisitionPlan("invalid", LIVE_ALIAS, LIVE_PATIENT, world.root)
    with pytest.raises(ValueError):
        AcquisitionPlan("n" * 43, "mock-alpha", LIVE_PATIENT, world.root)
    with pytest.raises(ValueError):
        GenuineOwnerCoordinatorApp(
            app._store,
            app.plan,
            replace(app.live, transport=httpx.MockTransport(lambda request: None)),
            world.gens,
            enabled=True,
            policy_accepted=True,
        )


def test_unchanged_genuine_gate_rejects_actual_synthetic_adapter_output(prepared):
    app, world, signer = prepared
    refuses(lambda: finish(app, signer, "acquire"), "genuine_candidate_required")
    assert app.plan.guard.exists() and world.staging.view(LIVE_ALIAS) is None
    assert world.catalog.status(LIVE_ALIAS, live=True)["attempt"]["state"] == "failed"
    assert world.catalog.status(LIVE_ALIAS, live=True)["attempt"]["operations"]["OAuth token"]["http_statuses"] == [200]
    assert len(world.epic.calls) == 9
    refuses(lambda: finish(app, signer, "acquire"), "acquisition_attempt_consumed")
    assert len(world.epic.calls) == 9


@pytest.mark.parametrize("change", ["same_root", "catalog_outside_plan"])
def test_storage_scope_requires_distinct_siblings_and_catalog_under_exact_root(prepared, monkeypatch, change):
    app, world, signer = prepared
    plan = app.plan
    if change == "same_root":
        monkeypatch.setattr(world.gens, "root", world.staging.root)
    else:
        plan = replace(plan, private_root=world.root / "different-approved-root")
        monkeypatch.setattr(world.gens, "root", plan.private_root / "generations")
        monkeypatch.setattr(world.staging, "root", plan.private_root / "staging")
    with pytest.raises(ValueError):
        GenuineOwnerCoordinatorApp(app._store, plan, app.live, world.gens, enabled=True, policy_accepted=True)
    assert world.epic.calls == [] and not app.plan.guard.exists()


def test_actual_fixture_acquire_review_candidate_bound_promotion_and_projection(prepared, monkeypatch):
    observations = [resource for resource in prepared[1].epic.records if resource["resourceType"] == "Observation"]
    observations[0]["meta"] = observations[0]["meta"] | {"lastUpdated": "2020-01-01T00:00:00Z"}
    observations[1]["meta"] = observations[1]["meta"] | {"lastUpdated": "2020-01-01T00:00:00.123Z"}
    app, world, signer, acquired_result = acquired(prepared, monkeypatch)
    candidate = acquired_result["result"]
    assert candidate["synthetic_transport"] is True
    assert candidate["patient_identifier"] == LIVE_PATIENT
    assert candidate["review"]["patient_display_name"].startswith("Invented")
    assert candidate["review"]["document_metadata_count"] > 0
    assert candidate["review"]["source_timestamps"]["Observation"] == {
        "count": 2,
        "oldest": "2020-01-01T00:00:00Z",
        "newest": "2020-01-01T00:00:00.123000Z",
    }
    assert set(candidate["review"]["operations"]) == LIVE_OPERATIONS | {"Binary", "MedicationRequest"}
    assert candidate["review"]["operations"]["Binary"]["execution"] == "not_requested"
    assert candidate["review"]["operations"]["MedicationRequest"]["execution"] == "not_requested"
    assert candidate["review"]["completeness"] == "unconfirmed"
    assert candidate["review"]["http_outcomes"] == {name: [200] for name in LIVE_OPERATIONS | {"OAuth token"}}
    assert len(world.epic.calls) == 9 and world.objects() == []
    review = finish(app, signer, "review")["result"]
    assert review["provenance"] == candidate["provenance"]
    approved = finish(app, signer, "promote", review["provenance"])["result"]
    assert approved["generation"]["state"] == "approved"
    assert approved["generation"]["generation_id"] == candidate["provenance"]["generation_id"]
    assert world.staging.view(LIVE_ALIAS) is None
    records = live_record_view(world.gens, LIVE_ALIAS, authorized=lambda: True, synthetic_only=True)
    assert records["provenance"]["manifest_sha256"] == candidate["provenance"]["manifest_sha256"]
    assert records["provenance"]["synthetic_transport"] is True
    assert records["record"]["encounters"] and records["record"]["conditions"]
    refuses(
        lambda: live_record_view(world.gens, LIVE_ALIAS, authorized=lambda: True, genuine_only=True),
        "genuine_evidence_required",
    )
    refuses(lambda: action(app, "read/options"), "live_reads_disabled")
    assert not app._live_reads_approved and approved["live_serving"] == "disabled"


def test_keyless_promotion_requires_the_reviewed_binding_and_cannot_acquire_or_serve(prepared, monkeypatch):
    acquisition, world, signer, result = acquired(prepared, monkeypatch)
    binding = result["result"]["provenance"]

    def reopen(approved):
        app = GenuineOwnerCoordinatorApp(
            acquisition._store,
            acquisition.plan,
            None,
            world.gens,
            enabled=True,
            policy_accepted=True,
            promotion_approved=True,
            approved_candidate=LiveEvidenceBinding(**approved),
        )
        app._auth.clock = world.clock
        return app

    wrong = reopen(binding | {"bundle_sha256": "f" * 64})
    assert action(wrong, "operations") == {"actions": ["review", "promote"]}
    refuses(lambda: action(wrong, "promote/options", binding), "candidate_not_approved")
    app = reopen(binding)
    refuses(lambda: action(app, "acquire/options"), "acquisition_not_approved")
    refuses(lambda: action(app, "read/options"), "live_reads_disabled")
    assert finish(app, signer, "review")["result"]["provenance"] == binding
    assert finish(app, signer, "promote", binding)["result"]["generation"]["state"] == "approved"
    assert len(world.epic.calls) == 9


@pytest.mark.parametrize("target", ["check", "obj", "final"])
def test_promotion_preserves_existing_exact_target(prepared, monkeypatch, target):
    app, world, signer, result = acquired(prepared, monkeypatch)
    binding = result["result"]["provenance"]
    gid = binding["generation_id"]
    path = world.gens.root / (f"objects/{gid}" if target == "final" else f"tmp/{gid}.{target}")
    path.write_bytes(b"preserve failed or unknown state")
    refuses(lambda: finish(app, signer, "promote", binding), "promotion_storage_occupied")
    assert path.read_bytes() == b"preserve failed or unknown state"


@pytest.mark.parametrize("readonly", [False, True])
def test_phase_reopen_does_not_recover_or_sweep_unselected_private_state(prepared, readonly):
    _, world, _ = prepared
    world.catalog.admit(
        LIVE_ALIAS,
        "other-intentionally-preserved",
        cooldown_s=0,
        patient_limit=1,
        global_limit=1,
        request_limit=1,
        policy=LIVE_POLICY_VERSION,
    )
    residue = world.gens.root / "tmp" / "unselected.obj"
    residue.write_bytes(b"preserve")
    location = world.root / "control.sqlite"
    # Close only the fixture connection here; the fixture can safely close it again.
    world.catalog.close()
    before = location.read_bytes()
    reopened = Catalog(location, world.clock, initialize=False, readonly=readonly)
    try:
        staging = files.LiveStaging(reopened, world.staging.root, initialize=False)
        LiveGenerations(reopened, world.gens.root, world.allow, staging, initialize=False)
        assert reopened.status(LIVE_ALIAS, live=True)["attempt"]["state"] == "queued"
    finally:
        reopened.close()
    assert location.read_bytes() == before and residue.read_bytes() == b"preserve"


@pytest.mark.parametrize("operation", ["review", "promote"])
def test_candidate_result_never_accessible_without_fresh_proof(prepared, monkeypatch, operation):
    app, world, signer, result = acquired(prepared, monkeypatch)
    body = result["result"]["provenance"] if operation == "promote" else None
    pending = signed(app, signer, operation, body)
    finish_result = action(app, operation + "/finish", pending)
    assert finish_result["result"]["operation"] == operation
    refuses(lambda: action(app, operation + "/finish", pending), "ceremony_rejected")
    assert not app._auth.sessions


@pytest.mark.parametrize("change", ["hash", "attempt", "generation", "bundle", "patient", "extra"])
def test_promotion_options_require_exact_previously_reviewed_binding(prepared, monkeypatch, change):
    app, world, signer, result = acquired(prepared, monkeypatch)
    body = dict(result["result"]["provenance"])
    if change == "extra":
        body["extra"] = "untrusted"
    elif change == "patient":
        body["patient_identifier"] = "invented-other"
    else:
        key = {
            "hash": "manifest_sha256",
            "attempt": "attempt_id",
            "generation": "generation_id",
            "bundle": "bundle_sha256",
        }[change]
        body[key] = "x" * len(body[key])
    refuses(lambda: action(app, "promote/options", body), "candidate_binding_required")
    assert world.objects() == []


@pytest.mark.parametrize("change", ["expire", "revoke", "flag", "plan", "marker", "wrong_operation"])
def test_fresh_proof_is_spent_and_changes_fail_before_promotion(prepared, monkeypatch, change):
    app, world, signer, result = acquired(prepared, monkeypatch)
    pending = signed(app, signer, "promote", result["result"]["provenance"])
    operation = "promote"
    if change == "expire":
        world.clock.now += 121
    elif change == "revoke":
        world.catalog.revoke(LIVE_ALIAS)
    elif change == "flag":
        app.promotion_approved = False
    elif change == "plan":
        app.plan = replace(app.plan, limits=replace(app.plan.limits, response_bytes=1000))
    elif change == "marker":
        monkeypatch.setattr(coordinator, "_require_genuine", REAL_GATE)
    elif change == "wrong_operation":
        operation = "review"
    with pytest.raises(RefreshRejected):
        action(app, operation + "/finish", pending)
    refuses(lambda: action(app, "promote/finish", pending), "ceremony_rejected")
    assert world.objects() == []


def test_single_attempt_guard_survives_new_app_and_never_retries(prepared, monkeypatch):
    app, world, signer, _ = acquired(prepared, monkeypatch)
    before = app.plan.guard.read_bytes()
    # Same approval id remains spent even if a NEW process tightens the approved bounds.
    other = GenuineOwnerCoordinatorApp(
        app._store,
        replace(app.plan, limits=replace(app.plan.limits, response_bytes=1000)),
        prepared[0].live,
        world.gens,
        enabled=True,
        policy_accepted=True,
        acquisition_approved=True,
    )
    other._auth.clock = world.clock
    refuses(lambda: finish(other, signer, "acquire"), "acquisition_attempt_consumed")
    assert app.plan.guard.read_bytes() == before and len(world.epic.calls) == 9


@pytest.mark.parametrize("operation", ["review", "promote"])
def test_reopen_changed_plan_cannot_review_or_promote_prior_candidate(prepared, monkeypatch, operation):
    app, world, signer, result = acquired(prepared, monkeypatch)
    other = GenuineOwnerCoordinatorApp(
        app._store,
        replace(app.plan, limits=replace(app.plan.limits, response_bytes=1000)),
        app.live,
        world.gens,
        enabled=True,
        policy_accepted=True,
        promotion_approved=True,
        approved_candidate=LiveEvidenceBinding(**result["result"]["provenance"]),
    )
    other._auth.clock = world.clock
    body = result["result"]["provenance"] if operation == "promote" else {}
    refuses(lambda: action(other, operation + "/options", body), "acquisition_scope_mismatch")
    assert world.objects() == []


@pytest.mark.parametrize("change", ["expired", "revoked"])
def test_after_await_acquisition_rechecks_authorization_before_retention_or_return(prepared, monkeypatch, change):
    app, world, signer = prepared
    synthetic_only(monkeypatch)
    parent = coordinator.LiveAcquisition

    class LateAdapter(parent):
        async def run(self):
            await super().run()
            if change == "expired":
                world.clock.now += 121
            else:
                world.catalog.revoke(LIVE_ALIAS)

    monkeypatch.setattr(coordinator, "LiveAcquisition", LateAdapter)
    with pytest.raises(RefreshRejected):
        finish(app, signer, "acquire")
    assert app.plan.guard.exists() and world.staging.view(LIVE_ALIAS) is None
    assert world.catalog.status(LIVE_ALIAS, live=True)["attempt"]["state"] == "failed"
    assert len(world.epic.calls) == 9


@pytest.mark.parametrize("gate", ["coordinator", "generation"])
def test_promotion_requires_both_independent_explicit_flags(prepared, monkeypatch, gate):
    app, world, signer, result = acquired(prepared, monkeypatch)
    if gate == "coordinator":
        app.promotion_approved = False
    else:
        world.gens.real_promotion = False
    refuses(lambda: action(app, "promote/options", result["result"]["provenance"]), "promotion_not_approved")
    assert world.objects() == [] and not app._live_reads_approved


@pytest.mark.parametrize("phase", ["before_stage", "candidate"])
def test_manifest_attempt_must_match_admitted_attempt_before_retention_review_or_promotion(
    prepared, monkeypatch, phase
):
    app, world, signer = prepared
    synthetic_only(monkeypatch)
    if phase == "before_stage":
        parent = coordinator.LiveAcquisition

        class WrongAttempt(parent):
            async def run(self):
                await super().run()
                self.candidate[0]["attempt_id"] = "wrong-invented-attempt"

        monkeypatch.setattr(coordinator, "LiveAcquisition", WrongAttempt)
        with pytest.raises(AcquisitionError) as error:
            finish(app, signer, "acquire")
        assert error.value.code == "attempt_scope"
        assert world.staging.view(LIVE_ALIAS) is None
    else:
        app, world, signer, result = acquired(prepared, monkeypatch)
        attempt = result["result"]["provenance"]["attempt_id"]
        _, raw, bundle = world.staging.read_candidate(attempt, LIVE_ALIAS)
        manifest = json.loads(raw)
        manifest["attempt_id"] = "wrong-invented-attempt"
        changed = encoded(manifest)
        (world.staging.root / attempt / "manifest.json").write_bytes(changed)
        with world.catalog.db:
            world.catalog.db.execute(
                "UPDATE live_candidate SET manifest_sha256 = ?, bytes = ? WHERE attempt_id = ?",
                (content_sha256(changed), len(changed) + len(bundle), attempt),
            )
        refuses(lambda: action(app, "review/options"), "candidate_integrity")
        refuses(lambda: action(app, "promote/options", result["result"]["provenance"]), "candidate_integrity")
    assert world.objects() == []


@pytest.mark.parametrize("change", ["expired", "revoked", "erased", "tampered"])
def test_post_header_candidate_fence_withholds_protected_body(prepared, monkeypatch, change):
    app, world, signer, result = acquired(prepared, monkeypatch)
    body = signed(app, signer, "review")

    def invalidate():
        if change == "expired":
            world.clock.now += 121
        elif change == "revoked":
            world.catalog.revoke(LIVE_ALIAS)
        elif change == "erased":
            world.staging.erase_all("test-only")
        else:
            attempt = result["result"]["provenance"]["attempt_id"]
            (world.staging.root / attempt / "manifest.json").write_text("{}")

    start, raw = http(app, "review/finish", body, on_start=invalidate)
    assert start["status"] == 200 and raw == b""


def test_acquire_http_result_uses_shared_security_boundary(prepared, monkeypatch):
    app, world, signer = prepared
    synthetic_only(monkeypatch)
    body = signed(app, signer, "acquire")
    start, raw = http(app, "acquire/finish", body, headers=[(b"cookie", b"invented=denied")])
    assert start["status"] == 400 and world.epic.calls == []
    start, raw = http(app, "acquire/finish", body)
    assert start["status"] == 200 and json.loads(raw)["result"]["synthetic_transport"] is True
    assert world.staging.view(LIVE_ALIAS)["state"] == "staged"
