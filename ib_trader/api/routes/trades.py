"""Trade group endpoints.

GET /api/trades — list all trade groups (filterable by status)
GET /api/trades/{serial} — get a single trade group by serial number
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query

from ib_trader.api.deps import get_trades, get_transactions
from ib_trader.api.serializers import TradeResponse
from ib_trader.data.models import LegType, TransactionAction
from ib_trader.data.repository import TradeRepository
from ib_trader.data.repositories.transaction_repository import TransactionRepository

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _serialize_trade(t, transactions: TransactionRepository) -> TradeResponse:
    """Serialize a TradeGroup plus fetch entry/exit fill detail from the
    transaction legs so the Trades panel can render qty/price/order_type
    without a second round-trip."""
    entry_qty: str | None = None
    entry_price: str | None = None
    entry_order_type: str | None = None
    exit_qty_total = Decimal("0")
    exit_notional = Decimal("0")

    entry_fill = transactions.get_entry_fill(t.id)
    if entry_fill is not None:
        if entry_fill.ib_filled_qty is not None:
            entry_qty = str(entry_fill.ib_filled_qty)
        if entry_fill.ib_avg_fill_price is not None:
            entry_price = str(entry_fill.ib_avg_fill_price)
        entry_order_type = entry_fill.order_type

    # CLOSE / exit fills across all legs — weighted avg across partials.
    for leg in transactions.get_filled_legs(t.id):
        if leg.leg_type == LegType.ENTRY:
            continue
        q = leg.ib_filled_qty or Decimal("0")
        if q <= 0:
            continue
        exit_qty_total += q
        if leg.ib_avg_fill_price is not None:
            exit_notional += q * leg.ib_avg_fill_price

    exit_qty: str | None = None
    exit_price: str | None = None
    if exit_qty_total > 0:
        exit_qty = str(exit_qty_total)
        if exit_notional > 0:
            exit_price = str((exit_notional / exit_qty_total).quantize(Decimal("0.0001")))

    # Prefer IB-authoritative realized P&L (set additively from
    # CommissionReport.realizedPNL) when available; fall back to the
    # engine/bot-computed value. Lets one-shot user orders show round-
    # trip P&L on close without colliding with bot-derived values.
    pnl = t.ib_realized_pnl if t.ib_realized_pnl is not None else t.realized_pnl
    return TradeResponse(
        id=t.id,
        serial_number=t.serial_number,
        symbol=t.symbol,
        direction=t.direction,
        status=t.status.value,
        realized_pnl=str(pnl) if pnl is not None else None,
        total_commission=str(t.total_commission) if t.total_commission is not None else None,
        opened_at=t.opened_at,
        closed_at=t.closed_at,
        entry_qty=entry_qty,
        entry_price=entry_price,
        exit_qty=exit_qty,
        exit_price=exit_price,
        order_type=entry_order_type,
    )


@router.get("", response_model=list[TradeResponse])
def list_trades(
    status: str | None = Query(None, description="Filter by status: open, closed, all"),
    trades: TradeRepository = Depends(get_trades),
    transactions: TransactionRepository = Depends(get_transactions),
):
    """List trade groups, optionally filtered by status."""
    if status == "open":
        rows = trades.get_open()
    elif status == "closed" or status is None:
        rows = trades.get_all()
    else:
        rows = trades.get_all()
    return [_serialize_trade(t, transactions) for t in rows]


@router.get("/{serial}", response_model=TradeResponse)
def get_trade(
    serial: int,
    trades: TradeRepository = Depends(get_trades),
    transactions: TransactionRepository = Depends(get_transactions),
):
    """Get a single trade group by serial number."""
    t = trades.get_by_serial(serial)
    if t is None:
        raise HTTPException(status_code=404, detail=f"Trade serial {serial} not found")
    return _serialize_trade(t, transactions)
