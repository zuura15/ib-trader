"""Contract tests for ``POST /engine/cancel-order`` (chart cancel button)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ib_trader.engine.internal_api import app, set_context


class _StubIB:
    def __init__(self, rows=None, cancel_error: Exception | None = None):
        self.rows = rows if rows is not None else [{
            "ib_order_id": "198019", "perm_id": 1647017791,
            "symbol": "MNQ", "local_symbol": "MNQZ6", "side": "SELL",
            "qty": Decimal("2"), "order_type": "LMT",
        }]
        self.cancel_error = cancel_error
        self.cancelled: list[str] = []

    async def get_open_orders(self):
        return self.rows

    async def cancel_order(self, ib_order_id):
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(ib_order_id)


def _ctx(ib):
    # transactions=None → the observational _write_txn fails and is
    # logged, never gating the cancel.
    return SimpleNamespace(ib=ib, redis=None, transactions=None)


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    set_context(None)


def _post(oid):
    return TestClient(app).post("/engine/cancel-order", json={"ib_order_id": oid})


def test_cancel_ok():
    ib = _StubIB()
    set_context(_ctx(ib))
    r = _post("198019")
    assert r.status_code == 200
    assert r.json()["status"] == "cancel_requested"
    assert ib.cancelled == ["198019"]


def test_permid_key_accepted():
    ib = _StubIB()
    set_context(_ctx(ib))
    assert _post("1647017791").status_code == 200
    assert ib.cancelled == ["1647017791"]


def test_unknown_order_404():
    ib = _StubIB(rows=[])
    set_context(_ctx(ib))
    assert _post("999").status_code == 404
    assert ib.cancelled == []


def test_foreign_order_is_409():
    ib = _StubIB(cancel_error=RuntimeError("bound to another session (TWS)"))
    set_context(_ctx(ib))
    r = _post("198019")
    assert r.status_code == 409
    assert "TWS" in r.json()["detail"]


def test_missing_id_422():
    set_context(_ctx(_StubIB()))
    assert _post("").status_code == 422
    assert _post("0").status_code == 422
