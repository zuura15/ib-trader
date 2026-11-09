"""ExecutionArchiveRepository: insert-once archive of IB executions."""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import Base
from ib_trader.data.repositories.execution_archive_repository import (
    ExecutionArchiveRepository,
)


@pytest.fixture
def repo():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return ExecutionArchiveRepository(scoped_session(sessionmaker(bind=engine)))


def _rec(eid, sym="GCZ6", t="2026-11-05T19:45:01+00:00", **kw):
    r = {
        "exec_id": eid, "perm_id": 123, "local_symbol": sym, "symbol": "GC",
        "sec_type": "FUT", "side": "BOT", "shares": "1.0", "price": "4123.9",
        "commission": "0", "realized_pnl": None, "multiplier": "100",
        "exec_time": t,
    }
    r.update(kw)
    return r


def test_insert_then_late_commission_fill_in(repo):
    assert repo.upsert([_rec("e1")]) == 1
    assert repo.upsert([_rec("e1")]) == 0                     # unchanged → no write
    assert repo.upsert([_rec("e1", commission="2.51", realized_pnl="0")]) == 1
    row = repo.get_for_symbol("GCZ6")[0]
    assert row.commission == Decimal("2.51") and row.realized_pnl == Decimal("0")
    assert row.exec_time == datetime(2026, 11, 5, 19, 45, 1)   # naive UTC
    # A later report never overwrites what's there.
    repo.upsert([_rec("e1", commission="9.99", price="1")])
    row = repo.get_for_symbol("GCZ6")[0]
    assert row.commission == Decimal("2.51") and row.price == Decimal("4123.9")


def test_get_for_symbol_filters_and_orders(repo):
    repo.upsert([
        _rec("b", t="2026-11-05T20:00:00+00:00"),
        _rec("a", t="2026-11-05T19:00:00+00:00"),
        _rec("c", sym="MGCZ6", t="2026-11-05T19:30:00+00:00"),
        _rec("d", t="2026-11-06T01:00:00+00:00"),
    ])
    assert [e.exec_id for e in repo.get_for_symbol("GCZ6")] == ["a", "b", "d"]
    before = datetime(2026, 11, 6, 0, 0)
    assert [e.exec_id for e in repo.get_for_symbol("GCZ6", before=before)] == ["a", "b"]
