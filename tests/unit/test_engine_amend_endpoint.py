"""Contract tests for ``POST /engine/amend-order`` (#99 drag-to-amend)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ib_trader.engine.internal_api import app, set_context


class _StubIB:
    def __init__(self, rows=None, amend_error: Exception | None = None):
        self.rows = rows if rows is not None else [{
            "ib_order_id": "198019", "perm_id": 1647017791,
            "symbol": "MNQ", "local_symbol": "MNQZ6", "side": "BUY",
            "qty": Decimal("20"), "order_type": "STP",
        }]
        self.amend_error = amend_error
        self.amended: list[tuple[str, Decimal]] = []

    async def get_open_orders(self):
        return self.rows

    async def amend_order(self, ib_order_id, new_price):
        if self.amend_error is not None:
            raise self.amend_error
        self.amended.append((ib_order_id, new_price))


def _ctx(ib):
    # transactions=None → the observational _write_txn fails and is
    # logged, never gating the amend (tenet: SQLite is observational).
    return SimpleNamespace(ib=ib, redis=None, transactions=None)


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    set_context(None)


class TestEngineAmend:
    def test_amend_ok(self):
        ib = _StubIB()
        set_context(_ctx(ib))
        r = TestClient(app).post(
            "/engine/amend-order",
            json={"ib_order_id": "198019", "price": "29650.25"},
        )
        assert r.status_code == 200
        assert ib.amended == [("198019", Decimal("29650.25"))]
        assert r.json()["price"] == "29650.25"

    def test_permid_key_accepted(self):
        ib = _StubIB()
        set_context(_ctx(ib))
        r = TestClient(app).post(
            "/engine/amend-order",
            json={"ib_order_id": "1647017791", "price": "29650.25"},
        )
        assert r.status_code == 200
        assert ib.amended[0][0] == "1647017791"

    def test_unknown_order_404(self):
        ib = _StubIB(rows=[])
        set_context(_ctx(ib))
        r = TestClient(app).post(
            "/engine/amend-order",
            json={"ib_order_id": "999", "price": "1.0"},
        )
        assert r.status_code == 404
        assert ib.amended == []

    def test_foreign_or_trail_refusal_is_409(self):
        ib = _StubIB(amend_error=RuntimeError("bound to another session (TWS)"))
        set_context(_ctx(ib))
        r = TestClient(app).post(
            "/engine/amend-order",
            json={"ib_order_id": "198019", "price": "29650.25"},
        )
        assert r.status_code == 409
        assert "TWS" in r.json()["detail"]

    def test_bad_price_422(self):
        set_context(_ctx(_StubIB()))
        c = TestClient(app)
        assert c.post("/engine/amend-order",
                      json={"ib_order_id": "1", "price": "abc"}).status_code == 422
        assert c.post("/engine/amend-order",
                      json={"ib_order_id": "1", "price": "-5"}).status_code == 422
