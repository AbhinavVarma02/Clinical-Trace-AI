"""One invented FHIR patient -> real guarded ASGI API -> exported UI contract. No listener/browser.

Reuse approved-alpha / invented-live-alpha and the existing FakeEpic, mapper, staging, generation and
projection. The optional receipt contains only synthetic API responses, never keys or real identity material.
"""

from __future__ import annotations

import asyncio
import json
import os
import selectors
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from api import beta_live_record as projection
from api.beta import create_mock_beta_app
from src.interop.epic import settings
from src.interop.epic.beta_catalog import MOCK_PATIENTS
from src.interop.epic.beta_live_acquisition import LiveRefresh
from src.interop.epic.beta_live_generations import GENERATION_RETENTION_S
from tests.test_beta_browser_bootstrap import READ, assets as assets_fixture
from tests.test_beta_generations import BODY
from tests.test_beta_live_generations import World
from tests.test_beta_live_record import approved, rewrite_synthetic
from tests.test_beta_live_route import LIVE_ALIAS, LIVE_PATIENT, FakeEpic
from tests.test_beta_owner_refresh import ORIGIN, harness as harness_fixture, keys as keys_fixture

PATIENTS = "/beta/epic/patients"
RECORD = f"{PATIENTS}/{LIVE_ALIAS}/record"
CAPTURE: dict = {"fixture": "INVENTED approved-alpha / invented-live-alpha", "failures": {}}


class _MemorySelector(selectors.SelectSelector):
    def select(self, timeout=None):
        if not self.get_map():
            if timeout:
                time.sleep(min(timeout, 0.001))
            return []
        raise AssertionError("synthetic ASGI check attempted descriptor I/O")


class _MemoryLoop(asyncio.SelectorEventLoop):
    """Single-threaded ASGI/MockTransport only: no Windows asyncio wakeup TCP listener/socketpair."""

    def __init__(self):
        super().__init__(_MemorySelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        if self._thread_id not in {None, threading.get_ident()}:
            raise AssertionError("synthetic ASGI check attempted cross-thread dispatch")


@pytest.fixture(scope="module", autouse=True)
def receipt():
    yield
    destination = os.environ.get("CT_SYNTHETIC_UI_RECEIPT")
    if destination and "view" in CAPTURE:
        Path(destination).write_text(json.dumps(CAPTURE, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def keys():
    return keys_fixture.__wrapped__()


@pytest.fixture
def assets(tmp_path):
    return assets_fixture.__wrapped__(tmp_path)


@pytest.fixture
def harness(tmp_path, keys):
    return harness_fixture.__wrapped__(tmp_path, keys)


@pytest.fixture
def slice_world(tmp_path, keys, harness, assets, monkeypatch):
    monkeypatch.setattr(asyncio.events, "new_event_loop", _MemoryLoop)
    monkeypatch.setenv(settings.ENABLE_ENV, "1")  # existing MockTransport seam only
    root = tmp_path / "invented-only"
    root.mkdir()
    world = World(root, keys[1], FakeEpic())
    gid = approved(world)
    harness.clock = world.clock.now

    def build(enabled=True):
        world.catalog.close()
        return create_mock_beta_app(
            root / "control.sqlite",
            oidc_key=keys[0].public_key(),
            epic_key=keys[1],
            oidc_transport=httpx.MockTransport(harness.oidc),
            epic_transport=httpx.MockTransport(harness.epic),
            browser_assets_root=assets,
            clock=world.clock,
            live=LiveRefresh(
                key=keys[1],
                config=settings.EpicConfig("invented-client", Path("never-read.key"), "https://keys.invalid/jwks"),
                allowlist=world.allow,
                transport=httpx.MockTransport(world.epic.respond),
            ),
            synthetic_live_records=enabled,
        )

    yield world, harness, build, gid
    world.catalog.close()


async def login(client, harness):
    start = await client.post("/beta/auth/login", headers={"Origin": ORIGIN})
    assert start.status_code == 303
    harness.auth_request = parse_qs(urlsplit(start.headers["location"]).query)
    result = await client.get(
        "/beta/auth/callback", params={"state": harness.auth_request["state"][0], "code": "invented-code"}
    )
    assert result.status_code == 200
    return result.json()["csrf_token"]


def checked(response, status=200, code=None):
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "access-control-allow-origin" not in response.headers
    if code:
        assert response.json() == {"error": {"code": code}}
    return {"status": response.status_code, "body": response.json()}


def test_api_exports_one_verified_patient_and_all_supported_resource_sections(slice_world):
    world, harness, build, gid = slice_world
    app = build()
    calls_before = len(world.epic.calls)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
                checked(await client.get(PATIENTS, headers=READ), 401, "owner_required")
                await login(client, harness)
                patients = checked(await client.get(PATIENTS, headers=READ))["body"]
                assert {p["alias"] for p in patients["patients"]} == {*MOCK_PATIENTS, LIVE_ALIAS}
                item = next(p for p in patients["patients"] if p["alias"] == LIVE_ALIAS)
                assert item["data_origin"] == "synthetic_live_fixture" and item["mode"] == "LIVE"
                view = checked(await client.get(RECORD, headers=READ))["body"]
                assert view["active"]["generation_id"] == view["provenance"]["generation_id"] == gid
                assert view["record"]["patient"]["display_name"] == "Synthetic LIVE fixture Alpha"
                assert {
                    s: view["record"][s]["total"]
                    for s in ("conditions", "encounters", "labs", "vitals", "reports", "notes")
                } == {"conditions": 2, "encounters": 1, "labs": 1, "vitals": 1, "reports": 1, "notes": 1}
                assert (view["mode"], view["serving"], view["publication"], view["completeness"]) == (
                    "LIVE",
                    "unavailable",
                    "disabled",
                    "unconfirmed",
                )
                assert view["record"]["notes"]["items"][0]["source"]["version"] == "upstream-7"
                for excluded in (BODY, "Binary/secret", LIVE_PATIENT, "never-read.key"):
                    assert excluded not in json.dumps(view)
                CAPTURE.update(
                    patients=patients,
                    view=view,
                    mock_view=checked(await client.get(f"{PATIENTS}/mock-alpha/record", headers=READ))["body"],
                    session=checked(await client.get("/beta/auth/session", headers=READ))["body"],
                )
                checked(
                    await client.get(RECORD.replace(LIVE_ALIAS, "approved-bravo"), headers=READ),
                    404,
                    "patient_unavailable",
                )

    asyncio.run(run())
    assert len(world.epic.calls) == calls_before and world.gens.real_promotion is False


@pytest.mark.parametrize(
    "change", ["real_marker", "hash", "tier", "mixed_patient", "inactive", "expired", "revoked", "erased"]
)
def test_discovery_and_record_both_withhold_invalid_or_unavailable_evidence(slice_world, change):
    world, harness, build, gid = slice_world
    if change == "real_marker":
        rewrite_synthetic(world, lambda m, b: m.update(synthetic_transport=False))
    elif change == "hash":
        path = world.gens.root / "objects" / gid / "manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif change == "tier":
        rewrite_synthetic(world, lambda m, b: m.update(mode="MOCK"))
    elif change == "mixed_patient":

        def corrupt(m, b):
            resource = next(e["resource"] for e in b["entry"] if e["resource"]["resourceType"] == "Condition")
            resource["subject"]["reference"] = "Patient/invented-other"

        rewrite_synthetic(world, corrupt)
    elif change == "inactive":
        with world.catalog.db:
            world.catalog.db.execute("UPDATE patient_head SET active = NULL WHERE alias = ?", (LIVE_ALIAS,))
    elif change == "expired":
        world.clock.now += GENERATION_RETENTION_S
        harness.clock = world.clock.now
    elif change == "revoked":
        world.catalog.revoke(LIVE_ALIAS)
    else:
        world.gens.erase_alias(LIVE_ALIAS, "synthetic-test")
    app = build()

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
                await login(client, harness)
                patients = checked(await client.get(PATIENTS, headers=READ))["body"]["patients"]
                assert {p["alias"] for p in patients} == set(MOCK_PATIENTS)
                code, status = {
                    "real_marker": ("real_promotion_disabled", 409),
                    "hash": ("generation_integrity", 409),
                    "tier": ("generation_integrity", 409),
                    "mixed_patient": ("generation_integrity", 409),
                    "revoked": ("patient_unavailable", 404),
                }.get(change, ("generation_unavailable", 404))
                CAPTURE["failures"][change] = checked(await client.get(RECORD, headers=READ), status, code)

    asyncio.run(run())


@pytest.mark.parametrize("route", [PATIENTS, RECORD])
def test_owner_expiry_during_projection_returns_no_patient_or_record(slice_world, monkeypatch, route):
    world, harness, build, gid = slice_world
    original = projection._record

    def expire(*args):
        result = original(*args)
        world.clock.now += 901
        return result

    monkeypatch.setattr(projection, "_record", expire)
    app = build()

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
                await login(client, harness)
                CAPTURE["failures"]["session_expired"] = checked(
                    await client.get(route, headers=READ), 401, "owner_required"
                )

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_normal_mock_list_and_record_stay_unchanged_outside_explicit_synthetic_opt_in(slice_world, enabled):
    world, harness, build, gid = slice_world
    app = build(enabled)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
                await login(client, harness)
                patients = checked(await client.get(PATIENTS, headers=READ))["body"]
                assert [p for p in patients["patients"] if p["alias"] in MOCK_PATIENTS] == [
                    {"alias": "mock-alpha", "label": "MOCK Alpha"},
                    {"alias": "mock-bravo", "label": "MOCK Bravo"},
                ]
                mock = checked(await client.get(f"{PATIENTS}/mock-alpha/record", headers=READ))["body"]
                assert mock["mode"] == "MOCK" and mock["record"] is None and mock["provenance"] is None
                if not enabled:
                    assert patients["mode"] == "MOCK" and len(patients["patients"]) == 2
                    checked(await client.get(RECORD, headers=READ), 404, "patient_unavailable")

    asyncio.run(run())


def test_discovered_synthetic_record_cannot_use_mock_generation_selection_or_clinical_ai(slice_world):
    world, harness, build, gid = slice_world
    app = build()

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
                csrf = await login(client, harness)
                assert (await client.get(RECORD, headers=READ)).status_code == 200
                headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
                assert (
                    await client.post(f"{PATIENTS}/{LIVE_ALIAS}/generations/{gid}/select", headers=headers)
                ).status_code == 404
                answer = await client.post(
                    f"{PATIENTS}/{LIVE_ALIAS}/generations/{gid}/runs",
                    headers=headers | {"Content-Type": "application/json"},
                    json={"question": "What is the latest creatinine?"},
                )
                assert answer.status_code == 400
                assert answer.json() == {"error": {"code": "unexpected_payload"}}  # no clinical/runtime route

    asyncio.run(run())
