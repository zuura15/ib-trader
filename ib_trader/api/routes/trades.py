"""Trade group endpoints.

GET /api/trades — list all trade groups (filterable by status)
GET /api/trades/{serial} — get a single trade group by serial number
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ib_trader.api.deps import get_trades
from ib_trader.api.serializers import TradeResponse
from ib_trader.data.repository import TradeRepository

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _serialize_trade(t) -> TradeResponse:
    return TradeResponse(
        id=t.id,
        serial_number=t.serial_number,
        symbol=t.symbol,
        direction=t.direction,
        status=t.status.value,
        realized_pnl=str(t.realized_pnl) if t.realized_pnl is not None else None,
        total_commission=str(t.total_commission) if t.total_commission is not None else None,
        opened_at=t.opened_at,
        closed_at=t.closed_at,
    )


@router.get("", response_model=list[TradeResponse])
def list_trades(
    status: str | None = Query(None, description="Filter by status: open, closed, all"),
    trades: TradeRepository = Depends(get_trades),
):
    """List trade groups, optionally filtered by status."""
    if status == "open":
        rows = trades.get_open()
    elif status == "closed" or status is None:
        rows = trades.get_all()
    else:
        rows = trades.get_all()
    return [_serialize_trade(t) for t in rows]


@router.get("/{serial}", response_model=TradeResponse)
def get_trade(
    serial: int,
    trades: TradeRepository = Depends(get_trades),
):
    """Get a single trade group by serial number."""
    t = trades.get_by_serial(serial)
    if t is None:
        raise HTTPException(status_code=404, detail=f"Trade serial {serial} not found")
    return _serialize_trade(t)
