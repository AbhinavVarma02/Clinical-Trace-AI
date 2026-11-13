"""Focused offline proof: worker preparation, owning-loop activation, drain-before-cleanup."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
import threading
import time
from contextlib import ExitStack
from dataclasses import replace

import pytest

from src.interop.epic.beta_generations import Generations
from tests.test_beta_generations import generations, harness, keys  # noqa: F401 - existing fixtures
from tests.test_beta_owner_refresh import ALPHA


def directories(h, expected=()):
    assert not any((h.generation_root / "staging").iterdir())
    for name in ("objects", "state"):
        assert {path.name for path in (h.generation_root / name).iterdir()} == set(expected)


def hold_preparation(monkeypatch, *, fail=False):
    entered, release = threading.Event(), threading.Event()
    original = Generations._prepare

    def held(*args):
        entered.set()
        assert release.wait(10), "test did not release preparation"
        if fail:
            raise OSError("invented preparation failure")
        return original(*args)

    monkeypatch.setattr(Generations, "_prepare", staticmethod(held))
    return entered, release


def test_preparation_yields_admission_and_reads_and_keeps_connection_ownership(generations, monkeypatch):  # noqa: F811
    from src.persistence.db import Database

    h = generations
    loop_threads, worker_threads, owners, closed = [], [], {}, []
    original_fence, original_prepare = Generations._fence, Generations._prepare
    original_init, original_close = Database.__init__, Database.close

    def fence(self, *args):
        loop_threads.append(threading.get_ident())
        return original_fence(self, *args)

    def prepare(*args):
        worker_threads.append(threading.get_ident())
        return original_prepare(*args)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        owners[id(self)] = threading.get_ident()

    def close(self):
        assert owners[id(self)] == threading.get_ident()
        closed.append(id(self))
        return original_close(self)

    monkeypatch.setattr(Generations, "_fence", fence)
    monkeypatch.setattr(Generations, "_prepare", staticmethod(prepare))
    monkeypatch.setattr(Database, "__init__", init)
    monkeypatch.setattr(Database, "close", close)
    entered, release = hold_preparation(monkeypatch)
    with h.generation_client() as client, concurrent.futures.ThreadPoolExecutor() as pool:
        h.login(client)
        response = pool.submit(h.post, client, ALPHA + "/refresh")
        try:
            assert entered.wait(5)
            assert response.result(2).status_code == 202
            status = pool.submit(client.get, ALPHA + "/status").result(2).json()
            assert status["attempt"]["state"] == "running"
            assert status["active"] is None
            busy = h.post(client, ALPHA + "/refresh", key="another-preparation-key")
            assert busy.status_code == 409 and busy.json()["error"]["code"] == "refresh_busy"
        finally:
            release.set()
        status = h.wait(client).json()
        assert status["attempt"]["state"] == "succeeded"
        gid = status["active"]["generation_id"]
        assert h.select_generation(client, gid).json()["generation_id"] == gid
        directories(h, [gid])
    assert len(set(loop_threads)) == 1
    assert len(worker_threads) == 1 and worker_threads[0] != loop_threads[0]
    assert closed and set(closed) == set(owners)


def test_quota_scan_tolerates_a_disappearing_worker_file(generations, monkeypatch):  # noqa: F811
    import src.interop.epic.beta_generations as storage

    h = generations
    original = storage._plain
    with h.generation_client() as client:
        h.login(client)
        transient = h.generation_root / "staging" / "transient-worker-wal"
        transient.write_bytes(b"synthetic temporary bytes")

        def vanish(path):
            if path == transient and transient.exists():
                transient.unlink()
            return original(path)

        monkeypatch.setattr(storage, "_plain", vanish)
        assert h.post(client, ALPHA + "/refresh").status_code == 202
        assert h.wait(client).json()["attempt"]["state"] == "succeeded"


@pytest.mark.parametrize("failure", ["deadline", "worker"])
def test_preparation_failure_keeps_the_previous_generation_and_erases_candidate(generations, monkeypatch, failure):  # noqa: F811
    h = generations
    with h.generation_client() as client:
        previous = h.refresh_generation(client)["active"]["generation_id"]
    if failure == "deadline":
        h.limits = replace(h.limits, deadline_s=0.5)
    entered, release = hold_preparation(monkeypatch, fail=failure == "worker")
    with h.generation_client() as client:
        h.clock += 3601
        h.login(client)
        try:
            assert h.post(client, ALPHA + "/refresh", key="preparation-failure-key").status_code == 202
            assert entered.wait(5)
            if failure == "deadline":
                time.sleep(0.55)
        finally:
            release.set()
        status = h.wait(client).json()
        assert status["attempt"]["state"] == "failed"
        assert status["attempt"]["error_code"] == ("deadline" if failure == "deadline" else "internal_error")
        assert status["active"]["generation_id"] == previous
        assert h.select_generation(client, previous, ack=True).status_code == 200
        directories(h, [previous])


@pytest.mark.parametrize("action", ["revoke", "logout", "shutdown", "repeat_cancel"])
def test_cancellation_drains_preparation_before_cleanup_or_catalog_close(generations, monkeypatch, action):  # noqa: F811
    h = generations
    entered, release = hold_preparation(monkeypatch)
    with ExitStack() as stack, concurrent.futures.ThreadPoolExecutor() as pool:
        client = stack.enter_context(h.generation_client())
        h.login(client)
        pending = None
        try:
            assert h.post(client, ALPHA + "/refresh").status_code == 202
            assert entered.wait(5)
            if action == "shutdown":
                pending = pool.submit(stack.close)
            elif action == "repeat_cancel":

                async def cancel_twice():
                    tasks = [
                        task
                        for task in asyncio.all_tasks()
                        if task.get_coro().__qualname__.endswith(".<locals>.acquire")
                    ]
                    assert len(tasks) == 1
                    tasks[0].cancel()
                    await asyncio.sleep(0)
                    tasks[0].cancel()
                    await asyncio.sleep(0)
                    assert not tasks[0].done()

                client.portal.call(cancel_twice)
            else:
                target = ALPHA + "/revoke" if action == "revoke" else "/beta/auth/logout"
                pending = pool.submit(h.post, client, target)
            if pending is not None:
                time.sleep(0.05)
                assert not pending.done(), "cancellation must wait for the active writer"
            if action in {"revoke", "logout"}:
                assert client.get(ALPHA + "/status").status_code == (404 if action == "revoke" else 401)
        finally:
            release.set()
        if pending is not None:
            result = pending.result(5)
            if action != "shutdown":
                assert result.status_code == 204
        if action == "repeat_cancel":
            assert h.wait(client).json()["attempt"]["state"] == "interrupted"
        stack.close()
    directories(h)
    with sqlite3.connect(h.db) as db:
        assert db.execute("SELECT COUNT(*) FROM generation").fetchone()[0] == 0
        state, error = db.execute("SELECT state, error_code FROM attempt").fetchone()
    assert state == "interrupted"
    assert error == ("authorization_revoked" if action in {"revoke", "logout"} else "shutdown")
