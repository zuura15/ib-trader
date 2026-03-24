"""Order endpoints.

GET /api/orders — list open orders
GET /api/orders/{ib_order_id} — get a single order by IB order ID
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import scoped_session

from ib_trader.api.deps import get_session_factory
from ib_trader.data.models import TransactionEvent
from ib_trader.data.repositories.transaction_repository import TransactionRepository

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _serialize_order(t: TransactionEvent) -> dict:
    """Serialize a TransactionEvent for the API response."""
    return {
        "id": str(t.ib_order_id or t.id),
        "symbol": t.symbol,
        "side": t.side,
        "qty_requested": str(t.quantity),
        "order_type": t.order_type,
        "status": t.action.value,
        "price_placed": str(t.price_placed) if t.price_placed else None,
        "ib_order_id": t.ib_order_id,
        "leg_type": t.leg_type.value if t.leg_type else None,
        "trade_serial": t.trade_serial,
        "placed_at": t.requested_at.isoformat() if t.requested_at else None,
        "ib_avg_fill_price": str(t.ib_avg_fill_price) if t.ib_avg_fill_price else None,
        "ib_filled_qty": str(t.ib_filled_qty) if t.ib_filled_qty else None,
        "commission": str(t.commission) if t.commission else None,
        "trade_id": t.trade_id,
    }


@router.get("")
def list_open_orders(sf: scoped_session = Depends(get_session_factory)):
    """List all open (non-terminal) orders."""
    rows = TransactionRepository(sf).get_open_orders()
    return [_serialize_order(t) for t in rows]


@router.get("/{ib_order_id}")
def get_order(ib_order_id: str, sf: scoped_session = Depends(get_session_factory)):
    """Get a single order by IB order ID."""
    t = TransactionRepository(sf).get_latest_by_ib_order_id(int(ib_order_id))
    if t is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return _serialize_order(t)
