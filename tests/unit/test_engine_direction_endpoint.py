"""Contract tests for ``POST /engine/direction/compute`` (#100)."""
from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from ib_trader.engine.internal_api import app, set_context


def _samples(rising=True, n=120):
    return deque((i * 2.0, 100 + (0.05 * i if rising else -0.05 * i))
                 for i in range(n))


def _ctx(**over):
    base = dict(
        settings={"direction_flat_ticks_per_min": 0.5,
                  "direction_llm_bar_seconds": 60},
        _direction_samples={"NQZ6": _samples()},
        _direction_contracts={"NQZ6": {"tick_size": "0.25"}},
        _direction_last_llm={"NQZ6": {"grok": {"status": "idle"},
                                      "jev": {"status": "idle"}}},
        redis=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset_context(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    yield
    set_context(None)


class TestDirectionCompute:
    def test_local_only_when_no_keys(self):
        set_context(_ctx())
        r = TestClient(app).post(
            "/engine/direction/compute", json={"symbol": "nqz6"})
        assert r.status_code == 200
        j = r.json()
        assert j["local"]["dir"] == "UP"
        assert j["grok"]["status"] == "off"
        assert j["jev"]["status"] == "off"
        assert j["samples"] == 120

    def test_unknown_symbol_409(self):
        set_context(_ctx())
        r = TestClient(app).post(
            "/engine/direction/compute", json={"symbol": "GCV6"})
        assert r.status_code == 409
        assert "direction_symbols" in r.json()["detail"]

    def test_grok_called_and_parsed(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "k")
        ctx = _ctx()
        set_context(ctx)
        with patch("ib_trader.signals.direction.query_openai_compatible",
                   new=AsyncMock(return_value="DOWN\n")) as q:
            r = TestClient(app).post(
                "/engine/direction/compute", json={"symbol": "NQZ6"})
        assert r.status_code == 200
        j = r.json()
        assert j["grok"]["status"] == "ok"
        assert j["grok"]["dir"] == "DOWN"
        assert j["grok"]["ms"] >= 0
        assert j["prompt"] and "NQZ6" in j["prompt"]
        # Verdict stashed for the sampling loop's republish.
        assert ctx._direction_last_llm["NQZ6"]["grok"]["dir"] == "DOWN"
        assert q.await_count == 1

    def test_jev_typed_choice_path(self, monkeypatch):
        monkeypatch.setenv("JEV_API_KEY", "k")
        ctx = _ctx(settings={"direction_flat_ticks_per_min": 0.5,
                             "direction_llm_bar_seconds": 60,
                             "direction_jev_base_url": "https://api.typesafe.ai",
                             "direction_jev_model": "jev-latest"})
        set_context(ctx)
        verdict = {"choice": "UP",
                   "probabilities": {"UP": 0.82, "DOWN": 0.05, "FLAT": 0.13},
                   "confidence": 0.71, "model": "jev-1.13.0"}
        with patch("ib_trader.signals.direction.query_typesafe_jev",
                   new=AsyncMock(return_value=verdict)) as q:
            r = TestClient(app).post(
                "/engine/direction/compute", json={"symbol": "NQZ6"})
        assert r.status_code == 200
        j = r.json()
        assert j["jev"]["status"] == "ok"
        assert j["jev"]["dir"] == "UP"
        assert j["jev"]["model"] == "jev-1.13.0"
        assert j["jev"]["probs"] == {"UP": 0.82, "DOWN": 0.05, "FLAT": 0.13}
        assert j["jev"]["raw"].startswith("UP=0.82")
        assert "conf=0.71" in j["jev"]["raw"]
        assert ctx._direction_last_llm["NQZ6"]["jev"]["dir"] == "UP"
        assert q.await_count == 1
        # Structured state, not the chat prompt, went to Jev.
        state = q.await_args.args[3]
        assert state["symbol"] == "NQZ6"
        assert state["closes_oldest_first"]

    def test_jev_off_choice_outside_contract_is_unparsed(self, monkeypatch):
        monkeypatch.setenv("JEV_API_KEY", "k")
        set_context(_ctx(settings={
            "direction_flat_ticks_per_min": 0.5,
            "direction_llm_bar_seconds": 60,
            "direction_jev_base_url": "https://api.typesafe.ai",
            "direction_jev_model": "jev-latest"}))
        verdict = {"choice": "SIDEWAYS", "probabilities": {},
                   "confidence": None, "model": "jev-1.13.0"}
        with patch("ib_trader.signals.direction.query_typesafe_jev",
                   new=AsyncMock(return_value=verdict)):
            r = TestClient(app).post(
                "/engine/direction/compute", json={"symbol": "NQZ6"})
        j = r.json()
        assert j["jev"]["status"] == "unparsed"
        assert j["jev"]["dir"] == "FLAT"

    def test_provider_error_is_reported_not_fatal(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "k")
        set_context(_ctx())
        with patch("ib_trader.signals.direction.query_openai_compatible",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            r = TestClient(app).post(
                "/engine/direction/compute", json={"symbol": "NQZ6"})
        assert r.status_code == 200
        j = r.json()
        assert j["grok"]["status"] == "err"
        assert j["local"]["dir"] == "UP"    # local verdict survives
