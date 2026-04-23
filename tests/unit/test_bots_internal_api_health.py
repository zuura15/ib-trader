"""Tests for ib-bots runner health endpoint.

The external pager (GH #47) polls GET /health on port 8082 every 60s
to verify the runner process is responsive. Must be lightweight — no
Redis, no bot state traversal — and must report active-bot count so
the pager summary can distinguish "runner alive, no bots running" (OK)
from "runner alive, 5 bots expected, 0 running" (investigate).
"""
import os

from fastapi.testclient import TestClient

from ib_trader.bots.internal_api import app, set_runner_state


def test_health_with_no_runner_state():
    """Before set_runner_state() is called (test harness / cold
    start), /health should still respond 200 and report 0 active."""
    # Reset module-level state cleanly.
    set_runner_state(None)  # type: ignore[arg-type]
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["pid"] == os.getpid()
    assert data["bots_active"] == 0


def test_health_counts_only_running_tasks():
    """bots_active counts tasks that are neither None nor done."""
    class _FakeTask:
        def __init__(self, done: bool):
            self._d = done
        def done(self) -> bool:
            return self._d

    set_runner_state({
        "running_tasks": {
            "bot-a": _FakeTask(done=False),   # active
            "bot-b": _FakeTask(done=False),   # active
            "bot-c": _FakeTask(done=True),    # finished → not counted
            "bot-d": None,                     # never started → not counted
        },
    })
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["bots_active"] == 2
    set_runner_state(None)  # type: ignore[arg-type]


def test_health_is_lightweight():
    """Must not read Redis or a session factory. If it did, this test
    wouldn't pass with the sentinel state below (no redis key set)."""
    set_runner_state({
        "running_tasks": {},
        # Deliberately NO "redis" / "session_factory" keys. /health
        # must not touch them.
    })
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    set_runner_state(None)  # type: ignore[arg-type]
