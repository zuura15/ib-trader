"""Unit tests for the Redis-backed runtime watchlist."""
from __future__ import annotations

import json

import pytest

from ib_trader.config import watchlist_runtime as wr
from ib_trader.redis.state import StateKeys


class FakeRedis:
    """Minimal async get/set stub backing a dict."""
    def __init__(self, initial: dict | None = None):
        self.store = dict(initial or {})

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, val):
        self.store[key] = val


def _seed_yaml(tmp_path, symbols):
    p = tmp_path / "watchlist.yaml"
    p.write_text("\n".join(f"- {s}" for s in symbols) + "\n")
    return str(p)


class TestResolve:
    @pytest.mark.asyncio
    async def test_seeds_from_yaml_when_absent(self, tmp_path):
        seed = _seed_yaml(tmp_path, ["QQQ", "GLD"])
        r = FakeRedis()
        out = await wr.resolve_watchlist_symbols(r, seed_path=seed)
        assert out == ["QQQ", "GLD"]
        # Persisted to Redis so the next read is authoritative.
        assert json.loads(r.store[StateKeys.watchlist_symbols()]) == ["QQQ", "GLD"]

    @pytest.mark.asyncio
    async def test_redis_value_wins_over_yaml(self, tmp_path):
        seed = _seed_yaml(tmp_path, ["QQQ"])
        r = FakeRedis({StateKeys.watchlist_symbols(): json.dumps(["META", "NVDA"])})
        out = await wr.resolve_watchlist_symbols(r, seed_path=seed)
        assert out == ["META", "NVDA"]  # not the YAML seed

    @pytest.mark.asyncio
    async def test_corrupt_value_reseeds(self, tmp_path):
        seed = _seed_yaml(tmp_path, ["SPY"])
        r = FakeRedis({StateKeys.watchlist_symbols(): "not-json"})
        out = await wr.resolve_watchlist_symbols(r, seed_path=seed)
        assert out == ["SPY"]

    @pytest.mark.asyncio
    async def test_redis_none_falls_back_to_yaml(self, tmp_path):
        seed = _seed_yaml(tmp_path, ["AMZN", "CRM"])
        out = await wr.resolve_watchlist_symbols(None, seed_path=seed)
        assert out == ["AMZN", "CRM"]


class TestSet:
    @pytest.mark.asyncio
    async def test_normalizes_and_persists(self):
        r = FakeRedis()
        out = await wr.set_watchlist_symbols(r, [" qqq ", "GLD", "qqq", ""])
        assert out == ["QQQ", "GLD"]  # upper, trimmed, de-duped, blanks dropped
        assert json.loads(r.store[StateKeys.watchlist_symbols()]) == ["QQQ", "GLD"]


class TestChartAnchors:
    def test_includes_configured_chart_bot_symbols(self):
        # Reads the real config/bots/*.yaml — chart contracts must appear.
        syms = wr.chart_anchor_symbols()
        assert isinstance(syms, list)
        # GCV6 / NQU6 / MGCV6 are current chart-bot symbols
        # (chart-bot-1 / -4 / -7). CL was dropped 2026-07-21.
        assert "GCV6" in syms
        assert "NQU6" in syms
        assert "MGCV6" in syms
        assert "CLU6" not in syms
