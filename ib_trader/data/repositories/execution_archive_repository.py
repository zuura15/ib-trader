"""Repository for the ``ib_executions`` archive (every IB fill, GH #101)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session, scoped_session

from ib_trader.data.models import IbExecution
from ib_trader.data.repository import safe_commit


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _naive_utc(ts: Any) -> datetime | None:
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


class ExecutionArchiveRepository:
    """Insert-once archive of IB executions, keyed by ``exec_id``."""

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def upsert(self, records: list[dict]) -> int:
        """Archive execution records (``exec_accumulator`` record shape).

        New exec_ids are inserted. An existing row only gains fields IB
        reports late — commission / realized P&L once the commission
        report pairs, multiplier when it was missing; nothing is
        overwritten. Returns the number of rows inserted or updated.
        """
        if not records:
            return 0
        s = self._session()
        ids = [r["exec_id"] for r in records if r.get("exec_id")]
        existing = {
            row.exec_id: row
            for row in s.query(IbExecution).filter(IbExecution.exec_id.in_(ids))
        }
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        touched = 0
        for r in records:
            eid = r.get("exec_id")
            if not eid:
                continue
            comm, rpnl = _dec(r.get("commission")), _dec(r.get("realized_pnl"))
            row = existing.get(eid)
            if row is None:
                shares, price = _dec(r.get("shares")), _dec(r.get("price"))
                ts = _naive_utc(r.get("exec_time"))
                if shares is None or price is None or ts is None or not r.get("local_symbol"):
                    continue
                s.add(IbExecution(
                    exec_id=eid, perm_id=int(r.get("perm_id") or 0) or None,
                    local_symbol=r["local_symbol"], symbol=r.get("symbol") or None,
                    sec_type=r.get("sec_type") or None, side=r.get("side") or "",
                    shares=shares, price=price, commission=comm,
                    realized_pnl=rpnl, multiplier=r.get("multiplier") or None,
                    exec_time=ts, recorded_at=now,
                ))
                touched += 1
                continue
            changed = False
            if comm is not None and comm != 0 and (row.commission is None or row.commission == 0):
                row.commission = comm
                changed = True
            if rpnl is not None and row.realized_pnl is None:
                row.realized_pnl = rpnl
                changed = True
            if r.get("multiplier") and not row.multiplier:
                row.multiplier = r["multiplier"]
                changed = True
            touched += changed
        if touched:
            safe_commit(s)
        return touched

    def get_for_symbol(
        self, local_symbol: str, *, before: datetime | None = None,
    ) -> list[IbExecution]:
        """Archived fills for one contract, oldest first; optionally only
        those strictly before ``before`` (naive UTC)."""
        q = (
            self._session().query(IbExecution)
            .filter(IbExecution.local_symbol == local_symbol)
        )
        if before is not None:
            q = q.filter(IbExecution.exec_time < before)
        return q.order_by(IbExecution.exec_time.asc(), IbExecution.exec_id.asc()).all()
