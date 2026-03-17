"""Positions endpoint.

GET /api/positions — returns current positions from IB via the position_cache
table, which the engine service refreshes every 30 seconds.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import text

from ib_trader.api.deps import get_session_factory

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("")
def list_positions(sf=Depends(get_session_factory)):
    """Return current broker positions from the position_cache table."""
    s = sf()
    try:
        rows = s.execute(text(
            "SELECT account_id, symbol, sec_type, quantity, avg_cost, broker, updated_at "
            "FROM position_cache ORDER BY symbol"
        )).fetchall()
    except Exception:
        return []

    return [
        {
            "account_id": r[0],
            "symbol": r[1],
            "sec_type": r[2],
            "quantity": str(r[3]),
            "avg_cost": str(r[4]),
            "broker": r[5],
            "updated_at": r[6],
        }
        for r in rows
    ]
