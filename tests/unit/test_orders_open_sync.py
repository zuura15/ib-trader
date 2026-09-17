"""Unit tests for the orders:open ⇄ IB truth sweep (#98)."""
from decimal import Decimal
from types import SimpleNamespace
import json

import pytest

from ib_trader.engine.main import sync_orders_open_from_ib
from ib_trader.redis.state import StateKeys


class FakeRedis:
    """Dict-backed async hash stub."""
    def __init__(self, initial: dict | None = None):
        self.h = dict(initial or {})

    async def hgetall(self, key):
        return dict(self.h)

    async def hset(self, key, field, value):
        self.h[field] = value

    async def hdel(self, key, field):
        self.h.pop(field, None)


class FakeIB:
    def __init__(self, rows):
        self.rows = rows

    async def get_open_orders(self):
        return self.rows


def _ctx(rows):
    return SimpleNamespace(ib=FakeIB(rows), settings={})


def _ib_stop_row(oid="777", local="GCV6"):
    return {
        "ib_order_id": oid,
        "symbol": "GC",
        "local_symbol": local,
        "side": "SELL",
        "qty": Decimal("2"),
        "order_type": "STP",
        "limit_price": None,
        "status": "PreSubmitted",
        "qty_filled": Decimal("0"),
        "avg_fill_price": None,
        "order_ref": "",
        "sec_type": "FUT",
        "con_id": 12345,
        "expiry": "20261028",
        "trading_class": "GC",
        "multiplier": "100",
        "stop_price": 4300.0,
        "trailing_percent": None,
    }


class TestOrdersOpenSync:
    @pytest.mark.asyncio
    async def test_ib_stop_upserted_and_zombie_purged(self):
        zombie = json.dumps({
            "ib_order_id": "92053", "symbol": "MGCQ6", "side": "SELL",
            "status": "Submitted", "target_qty": "8",
        })
        r = FakeRedis({"92053": zombie})
        upserts, removals = await sync_orders_open_from_ib(
            _ctx([_ib_stop_row()]), r,
        )
        assert upserts == 1
        assert removals == 1
        assert "92053" not in r.h            # zombie gone
        row = json.loads(r.h["777"])
        assert row["symbol"] == "GCV6"       # localSymbol-authoritative
        assert row["order_type"] == "STP"
        assert row["stop_price"] == 4300.0
        assert row["status"] == "PreSubmitted"
        assert row["target_qty"] == "2"
        assert row["terminal"] is False

    @pytest.mark.asyncio
    async def test_second_pass_is_noop(self):
        r = FakeRedis()
        ctx = _ctx([_ib_stop_row()])
        await sync_orders_open_from_ib(ctx, r)
        ts1 = json.loads(r.h["777"])["ts"]
        upserts, removals = await sync_orders_open_from_ib(ctx, r)
        assert (upserts, removals) == (0, 0)
        assert json.loads(r.h["777"])["ts"] == ts1  # ts preserved

    @pytest.mark.asyncio
    async def test_event_path_enrichment_survives_merge(self):
        prev = json.dumps({
            "ib_order_id": "777", "symbol": "GCV6", "side": "SELL",
            "status": "Submitted", "orderRef": "IBT:console:GCV6:SELL:9",
            "ts": "2026-09-15T00:00:00+00:00",
        })
        r = FakeRedis({"777": prev})
        await sync_orders_open_from_ib(_ctx([_ib_stop_row()]), r)
        row = json.loads(r.h["777"])
        assert row["orderRef"] == "IBT:console:GCV6:SELL:9"
        assert row["ts"] == "2026-09-15T00:00:00+00:00"
        assert row["stop_price"] == 4300.0   # IB values win

    @pytest.mark.asyncio
    async def test_phantom_and_blank_rows_skipped(self):
        rows = [
            {**_ib_stop_row(oid="0")},
            {**_ib_stop_row(oid="778", local=""), "symbol": ""},
        ]
        r = FakeRedis()
        upserts, removals = await sync_orders_open_from_ib(_ctx(rows), r)
        assert (upserts, removals) == (0, 0)
        assert r.h == {}

    @pytest.mark.asyncio
    async def test_permid_rekey_migrates_row(self):
        # Restart / TWS-modify re-bind: same working order returns under
        # a different key (permId) — the old row must migrate, keeping
        # event-path enrichment, with no duplicate and no zombie purge.
        prev = json.dumps({
            "ib_order_id": "198019", "symbol": "MNQZ6", "side": "BUY",
            "status": "PreSubmitted", "perm_id": 1647017791,
            "orderRef": "IBT:console:MNQZ6:BUY:12",
            "ts": "2026-09-17T04:47:10+00:00",
        })
        r = FakeRedis({"198019": prev})
        row_ib = {
            **_ib_stop_row(oid="1647017791", local="MNQZ6"),
            "perm_id": 1647017791,
        }
        upserts, removals = await sync_orders_open_from_ib(_ctx([row_ib]), r)
        assert upserts == 1
        assert removals == 0                  # migration, not purge
        assert "198019" not in r.h
        assert set(r.h) == {"1647017791"}     # exactly one row
        row = json.loads(r.h["1647017791"])
        assert row["orderRef"] == "IBT:console:MNQZ6:BUY:12"
        assert row["ts"] == "2026-09-17T04:47:10+00:00"
        assert row["perm_id"] == 1647017791
        assert row["stop_price"] == 4300.0

    @pytest.mark.asyncio
    async def test_empty_ib_clears_hash(self):
        r = FakeRedis({"1": json.dumps({"ib_order_id": "1", "symbol": "X"})})
        upserts, removals = await sync_orders_open_from_ib(_ctx([]), r)
        assert (upserts, removals) == (0, 1)
        assert r.h == {}
