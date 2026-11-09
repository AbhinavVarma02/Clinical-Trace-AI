"""Fake-time proof of passive expiry and authorized action-only session renewal."""

from __future__ import annotations

import pytest

from tests.test_beta_browser_bootstrap import (
    READ,
    SESSION,
    PATIENTS,
    browser as browser_fixture,
    assets as assets_fixture,
    require,
)
from tests.test_beta_generations import generations as generations_fixture
from tests.test_beta_owner_refresh import ALPHA, ORIGIN, harness as owner_fixture, keys as keys_fixture

browser = browser_fixture
assets = assets_fixture
generations = generations_fixture
harness = owner_fixture
keys = keys_fixture


def test_passive_legacy_get_does_not_extend_idle(harness):
    with harness.client() as client:
        harness.login(client)
        harness.clock += 899
        require(client.get(ALPHA + "/status").status_code == 200, "pre-deadline-read")
        harness.clock += 1
        require(client.get(ALPHA + "/status").status_code == 401, "passive-deadline")
    require(not harness.calls, "no-acquisition")


def test_failed_action_does_not_extend_idle(harness):
    with harness.client() as client:
        harness.login(client)
        harness.clock += 899
        require(harness.post(client, ALPHA + "/refresh", key="bad").status_code == 400, "invalid-key-denied")
        harness.clock += 1
        require(client.get(ALPHA + "/status").status_code == 401, "failed-action-no-heartbeat")
    require(not harness.calls, "failed-action-no-acquisition")


def test_session_and_patient_polling_never_extend_idle(browser):
    h, context = browser
    with context as client:
        h.login(client)
        created = h.clock
        for offset in range(0, 900, 30):
            h.clock = created + offset
            value = client.get(SESSION, headers=READ).json()
            require(
                value["last_activity_at_unix_s"] == created and value["idle_expires_at_unix_s"] == created + 900,
                "passive-metadata",
            )
            require(client.get(PATIENTS, headers=READ).status_code == 200, "passive-list")
            require(client.get(ALPHA + "/status").status_code == 200, "passive-status")
            require(client.get("/beta/app/").status_code == 200, "passive-static")
        h.clock = created + 900
        require(client.get(SESSION, headers=READ).status_code == 401, "exact-idle-expiry")
    require(not h.calls and len(h.oidc_calls) == 1, "polls-no-provider")


def test_new_refresh_renews_but_duplicate_does_not(browser):
    h, context = browser
    with context as client:
        h.login(client)
        created = h.clock
        h.clock += 899
        require(h.post(client, ALPHA + "/refresh").status_code == 202, "refresh-admission")
        require(h.wait(client).status_code == 200, "refresh-completion")
        value = client.get(SESSION, headers=READ).json()
        require(
            value["last_activity_at_unix_s"] == created + 899 and value["created_at_unix_s"] == created,
            "new-action-touch",
        )
        calls = len(h.calls)
        h.clock = created + 1798
        require(h.post(client, ALPHA + "/refresh").status_code == 202, "duplicate-attempt-response")
        require(
            client.get(SESSION, headers=READ).json()["last_activity_at_unix_s"] == created + 899, "duplicate-no-touch"
        )
        h.clock += 1
        require(
            client.get(SESSION, headers=READ).status_code == 401 and len(h.calls) == calls,
            "duplicate-no-replay-or-renewal",
        )


def test_authorized_generation_selection_preserves_absolute_expiry(generations):
    h = generations
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
        created, calls = h.clock, len(h.calls)
        for offset in range(899, 28800, 899):
            h.clock = created + offset
            require(h.select_generation(client, gid).status_code == 200, "explicit-action-renewal")
        h.clock = created + 28799
        require(client.get(ALPHA + "/status").status_code == 200, "absolute-pre-boundary")
        h.clock = created + 28800
        require(h.select_generation(client, gid).status_code == 401, "absolute-deadline-with-activity")
        require(len(h.calls) == calls, "selection-no-acquisition")


@pytest.mark.parametrize("action", ["run", "review", "duplicate-review", "csrf", "cache", "denied-review", "rollback"])
def test_generation_action_activity_boundaries(generations, action):
    h = generations
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
        path = f"{ALPHA}/generations/{gid}"
        run = h.run_generation(client, gid).json()
        if action == "duplicate-review":
            require(h.post(client, path + f"/runs/{run['run_id']}/review").status_code == 200, "initial-review")
        h.login(client)
        created = h.clock
        h.clock += 899 if action != "rollback" else 299
        renewed = action in {"run", "review", "rollback"}
        if action == "run":
            response = h.run_generation(client, gid)
        elif action in {"review", "duplicate-review"}:
            response = h.post(client, path + f"/runs/{run['run_id']}/review")
        elif action == "rollback":
            response = h.post(client, path + "/rollback")
        elif action == "csrf":
            response = client.post(path + "/select", headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"})
        elif action == "cache":
            require(h.post(client, ALPHA + "/refresh", key="new-background-attempt").status_code == 202, "admit-next")
            require(h.wait(client).status_code == 200, "finish-next")
            # Establish a fresh session so the refresh itself is not the heartbeat being tested.
            h.login(client)
            created = h.clock
            h.clock += 899
            response = h.select_generation(client, gid)
        else:
            review = h.post(client, path + f"/runs/{run['run_id']}/review").json()
            h.login(client)
            created = h.clock
            h.clock += 899
            response = h.post(client, path + f"/reviews/{review['review_id']}/decide")
        require(
            response.status_code
            == (
                200 if renewed or action == "duplicate-review" else 403 if action in {"csrf", "denied-review"} else 409
            ),
            "action-outcome",
        )
        touch_at = h.clock
        h.clock = created + 900
        require(client.get(ALPHA + "/status").status_code == (200 if renewed else 401), "action-only-idle-renewal")
        if renewed:
            h.clock = touch_at + 900
            require(client.get(ALPHA + "/status").status_code == 401, "renewed-idle-still-bounded")
