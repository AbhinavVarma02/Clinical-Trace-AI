"""Default-disabled Beta record GET integration. Only invented, explicitly LIVE-shaped offline fixtures are used.

Seed via the existing test-only adapter/staging/approval path, close its catalog, then start the actual Beta app.
No real authentication, credential, Epic request, promotion enablement or normal-launcher opt-in is involved.
"""

from __future__ import annotations

from inspect import signature
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api import beta_live_record as projection
from api.beta import create_mock_beta_app
from src.interop.epic import settings
from src.interop.epic.beta_catalog import MOCK_PATIENTS, RefreshRejected
from src.interop.epic.beta_live_acquisition import LiveRefresh
from src.interop.epic.beta_live_generations import GENERATION_RETENTION_S
from src.interop.epic.beta_live_staging import LiveAllowlist
from tests.offline_network import blocked_events
from tests.test_beta_browser_bootstrap import READ, assets as assets_fixture
from tests.test_beta_live_generations import World
from tests.test_beta_live_record import approved, read, rewrite_synthetic
from tests.test_beta_live_route import LIVE_ALIAS, LIVE_PATIENT, FakeEpic
from tests.test_beta_owner_refresh import ORIGIN, harness as harness_fixture, keys as keys_fixture

assets = assets_fixture
harness = harness_fixture
keys = keys_fixture
RECORD = f"/beta/epic/patients/{LIVE_ALIAS}/record"


@pytest.fixture
def seeded(tmp_path, keys, harness, assets, monkeypatch):
    monkeypatch.setenv(settings.ENABLE_ENV, "1")  # existing adapter test double; never a real transport
    root = tmp_path / "synthetic-live-api"
    root.mkdir(mode=0o700)
    world = World(root, keys[1], FakeEpic())
    gid = approved(world)
    expected = read(world)
    harness.clock = world.clock.now

    class Seeded:
        closed = False

        def client(self, *, enabled=None, live=True):
            world.catalog.close()
            self.closed = True
            app = create_mock_beta_app(
                root / "control.sqlite",
                oidc_key=keys[0].public_key(),
                epic_key=keys[1],
                oidc_transport=httpx.MockTransport(harness.oidc),
                epic_transport=httpx.MockTransport(harness.epic),
                browser_assets_root=assets,
                live=self.refresh if live else None,
                clock=world.clock,
                **({"synthetic_live_records": enabled} if enabled is not None else {}),
            )
            return TestClient(app, base_url=ORIGIN, follow_redirects=False)

    state = Seeded()
    state.world, state.harness, state.assets, state.gid, state.expected = world, harness, assets, gid, expected
    state.refresh = LiveRefresh(
        key=keys[1],
        config=settings.EpicConfig("invented-live-client", Path("never-read.key"), "https://keys.invalid/jwks"),
        allowlist=world.allow,
        transport=httpx.MockTransport(world.epic.respond),
    )
    yield state
    if not state.closed:
        world.catalog.close()
    assert not blocked_events


def assert_error(response, status, code):
    assert response.status_code == status
    if response.request.method != "HEAD":
        assert response.json() == {"error": {"code": code}}
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers


def test_opt_in_get_returns_exact_verified_record_contract_and_requests_no_epic_data(seeded, monkeypatch):
    original = projection.live_record_view
    calls = []

    def require_synthetic(*args, **kwargs):
        assert kwargs["synthetic_only"] is True
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(projection, "live_record_view", require_synthetic)
    world = seeded.world
    requests_before = len(world.epic.calls)
    evidence_before = {str(path): path.read_bytes() for path in world.gens.root.rglob("*") if path.is_file()}
    with seeded.client(enabled=True) as client:
        seeded.harness.login(client)
        response = client.get(RECORD, headers=READ)
        assert response.status_code == 200 and response.json() == seeded.expected
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert "access-control-allow-origin" not in response.headers
        view = response.json()
        assert (view["mode"], view["data_origin"], view["completeness"]) == (
            "LIVE",
            "synthetic_live_fixture",
            "unconfirmed",
        )
        assert (view["serving"], view["publication"]) == ("unavailable", "disabled")
        assert view["active"]["generation_id"] == view["provenance"]["generation_id"] == seeded.gid
        assert world.gens.real_promotion is False
        assert len(calls) == 1 and len(world.epic.calls) == requests_before
    assert evidence_before == {str(path): path.read_bytes() for path in world.gens.root.rglob("*") if path.is_file()}
    assert world.temporary() == []


@pytest.mark.parametrize("enabled,live", [(None, True), (False, True), (None, False)])
def test_normal_factory_remains_closed_to_live_and_mock_contract_is_unchanged(seeded, enabled, live):
    assert signature(create_mock_beta_app).parameters["synthetic_live_records"].default is False
    with seeded.client(enabled=enabled, live=live) as client:
        seeded.harness.login(client)
        assert_error(client.get(RECORD, headers=READ), 404, "patient_unavailable")
        patients = client.get("/beta/epic/patients", headers=READ).json()
        assert {patient["alias"] for patient in patients["patients"]} == set(MOCK_PATIENTS)
        mock = client.get("/beta/epic/patients/mock-alpha/record", headers=READ)
        assert mock.status_code == 200 and mock.json()["mode"] == "MOCK" and mock.json()["record"] is None
        # Request headers cannot turn on the constructor-only switch.
        assert_error(
            client.get(RECORD, headers=READ | {"X-Beta-Synthetic-Live-Records": "true"}), 404, "patient_unavailable"
        )


@pytest.mark.parametrize("change", ["no_live", "real_transport", "other_transport", "no_assets", "integer", "string"])
def test_synthetic_opt_in_refuses_unsafe_construction_before_opening_any_store(tmp_path, keys, assets, harness, change):
    refresh = LiveRefresh(
        key=keys[1],
        config=settings.EpicConfig("invented-client", Path("never-read.key"), "https://keys.invalid/jwks"),
        allowlist=LiveAllowlist({LIVE_ALIAS: LIVE_PATIENT}),
        transport=None
        if change == "real_transport"
        else (object() if change == "other_transport" else httpx.MockTransport(harness.epic)),
    )
    path = tmp_path / "must-not-open.sqlite"
    with pytest.raises(ValueError):
        create_mock_beta_app(
            path,
            oidc_key=keys[0].public_key(),
            epic_key=keys[1],
            oidc_transport=httpx.MockTransport(harness.oidc),
            epic_transport=httpx.MockTransport(harness.epic),
            live=None if change == "no_live" else refresh,
            browser_assets_root=None if change == "no_assets" else assets,
            synthetic_live_records={"integer": 1, "string": "true"}.get(change, True),
        )
    assert not path.exists() and not harness.calls and not harness.oidc_calls


def test_opt_in_still_requires_the_owner_session(seeded):
    with seeded.client(enabled=True) as client:
        assert_error(client.get(RECORD, headers=READ), 401, "owner_required")
        seeded.harness.login(client)
        assert client.get(RECORD, headers=READ).status_code == 200


@pytest.mark.parametrize(
    "attack,status,code",
    [
        ("profile", 403, "same_origin_required"),
        ("origin", 403, "same_origin_required"),
        ("duplicate_origin", 403, "same_origin_required"),
        ("host", 403, "invalid_origin"),
        ("http", 403, "invalid_origin"),
        ("spoof", 400, "untrusted_header"),
        ("query", 400, "unexpected_payload"),
        ("body", 400, "unexpected_payload"),
        ("post", 405, "method_not_allowed"),
        ("head", 405, "method_not_allowed"),
    ],
)
def test_opt_in_uses_the_existing_browser_origin_payload_and_header_boundary(seeded, attack, status, code):
    with seeded.client(enabled=True) as client:
        seeded.harness.login(client)
        headers, method, url, extra = dict(READ), "GET", RECORD, {}
        if attack == "profile":
            headers.pop("X-Beta-Client", None)
            headers.pop("x-beta-client", None)
        elif attack == "origin":
            headers["Origin"] = "https://other.invalid"
        elif attack == "duplicate_origin":
            headers = list(headers.items()) + [("Origin", ORIGIN), ("origin", ORIGIN)]
        elif attack == "host":
            headers["Host"] = "other.invalid"
        elif attack == "http":
            url = "http://beta.invalid" + RECORD
        elif attack == "spoof":
            headers["X-Owner"] = "invented-owner"
        elif attack == "query":
            url += "?synthetic_live_records=true"
        elif attack == "body":
            extra["content"] = b"unexpected"
        else:
            method = attack.upper()
        assert_error(client.request(method, url, headers=headers, **extra), status, code)


@pytest.mark.parametrize(
    "change,status,code",
    [
        ("manifest", 409, "generation_integrity"),
        ("bundle", 409, "generation_integrity"),
        ("tier", 409, "generation_integrity"),
        ("mixed_patient", 409, "generation_integrity"),
        ("real_label", 409, "real_promotion_disabled"),
        ("inactive", 404, "generation_unavailable"),
        ("expired", 404, "generation_unavailable"),
        ("erased", 404, "generation_unavailable"),
        ("revoked", 404, "patient_unavailable"),
    ],
)
def test_record_get_rejects_unapproved_unavailable_tampered_or_non_synthetic_evidence(seeded, change, status, code):
    world = seeded.world
    directory = world.gens.root / "objects" / seeded.gid
    if change in {"manifest", "bundle"}:
        path = directory / ("manifest.json" if change == "manifest" else "normalized_bundle.json")
        path.write_bytes(path.read_bytes() + b" ")
    elif change == "tier":
        rewrite_synthetic(world, lambda manifest, bundle: manifest.update(mode="MOCK"))
    elif change == "mixed_patient":

        def corrupt(manifest, bundle):
            resource = next(
                entry["resource"] for entry in bundle["entry"] if entry["resource"]["resourceType"] == "Condition"
            )
            resource["subject"]["reference"] = "Patient/synthetic_other"

        rewrite_synthetic(world, corrupt)
    elif change == "real_label":
        rewrite_synthetic(world, lambda manifest, bundle: manifest.update(synthetic_transport=False))
    elif change == "inactive":
        with world.catalog.db:
            world.catalog.db.execute("UPDATE patient_head SET active = NULL WHERE alias = ?", (LIVE_ALIAS,))
    elif change == "expired":
        world.clock.now += GENERATION_RETENTION_S
        seeded.harness.clock = world.clock.now
    elif change == "erased":
        world.gens.erase_alias(LIVE_ALIAS, "test_owner_request")
    else:
        world.catalog.revoke(LIVE_ALIAS)
    with seeded.client(enabled=True) as client:
        seeded.harness.login(client)
        assert_error(client.get(RECORD, headers=READ), status, code)


@pytest.mark.parametrize("action", ["logout", "patient", "owner", "erase"])
def test_existing_logout_revocation_and_erasure_remove_api_access(seeded, action):
    with seeded.client(enabled=True) as client:
        seeded.harness.login(client)
        assert client.get(RECORD, headers=READ).status_code == 200
        if action == "owner":
            client.portal.call(client.app.state.revoke_owner)
        else:
            path = {
                "logout": "/beta/auth/logout",
                "patient": f"/beta/epic/patients/{LIVE_ALIAS}/revoke",
                "erase": f"/beta/epic/patients/{LIVE_ALIAS}/generation/erase",
            }[action]
            assert seeded.harness.post(client, path).status_code == 204
        code, status = (
            ("owner_required", 401)
            if action in {"logout", "owner"}
            else (("patient_unavailable", 404) if action == "patient" else ("generation_unavailable", 404))
        )
        assert_error(client.get(RECORD, headers=READ), status, code)


def test_session_expiry_during_projection_does_not_return_the_record(seeded, monkeypatch):
    original = projection._record

    def expire(*args):
        result = original(*args)
        seeded.world.clock.now += 901
        return result

    monkeypatch.setattr(projection, "_record", expire)
    with seeded.client(enabled=True) as client:
        seeded.harness.login(client)
        assert_error(client.get(RECORD, headers=READ), 401, "owner_required")


def test_synthetic_live_alias_discovery_still_excludes_mock_generation_routes(seeded):
    with seeded.client(enabled=True) as client:
        seeded.harness.login(client)
        assert client.get(RECORD, headers=READ).status_code == 200
        patients = client.get("/beta/epic/patients", headers=READ).json()
        assert {patient["alias"] for patient in patients["patients"]} == {*MOCK_PATIENTS, LIVE_ALIAS}
        assert next(patient for patient in patients["patients"] if patient["alias"] == LIVE_ALIAS) == {
            "alias": LIVE_ALIAS,
            "label": "Synthetic LIVE-shaped fixture",
            "mode": "LIVE",
            "data_origin": "synthetic_live_fixture",
        }
        assert_error(client.get(RECORD.replace(LIVE_ALIAS, "approved-bravo"), headers=READ), 404, "patient_unavailable")
        path = f"/beta/epic/patients/{LIVE_ALIAS}/generations/{seeded.gid}/select"
        assert seeded.harness.post(client, path).status_code == 404
        mock = client.get("/beta/epic/patients/mock-alpha/record", headers=READ).json()
        assert mock["mode"] == "MOCK" and mock["provenance"] is None and mock["record"] is None


def test_fixture_only_guard_runs_before_any_projection_of_a_non_synthetic_marker(monkeypatch):
    class TestOnlyVerifiedReader:
        def project_active(self, alias, authorized, project):
            assert authorized()
            return project({"synthetic_transport": False}, b"invented marker only; not real evidence", {})

    def never_read(*args):
        pytest.fail("synthetic-only gate let a non-synthetic marker reach FHIR projection")

    monkeypatch.setattr(projection, "_resources", never_read)
    with pytest.raises(RefreshRejected) as error:
        projection.live_record_view(TestOnlyVerifiedReader(), LIVE_ALIAS, authorized=lambda: True, synthetic_only=True)
    assert (error.value.status, error.value.code) == (409, "synthetic_evidence_required")


def test_mock_launcher_has_no_opt_in_or_live_transport(tmp_path, assets, monkeypatch):
    import api.beta_local as local

    calls = []

    def capture(*args, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(local, "create_mock_beta_app", capture)
    local.build_local_app(tmp_path, assets)
    assert len(calls) == 1 and calls[0].get("synthetic_live_records", False) is False
    assert "live" not in calls[0]
    root = Path(__file__).resolve().parents[1]
    assert "synthetic_live_records" not in (root / "scripts/run_beta_local.py").read_text(encoding="utf-8")
