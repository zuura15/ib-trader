"""Order endpoints.

GET /api/orders — list open orders
GET /api/orders/{order_id} — get a single order
"""
from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_orders
from ib_trader.api.serializers import OrderResponse
from ib_trader.data.repository import OrderRepository

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _serialize_order(o) -> OrderResponse:
    return OrderResponse(
        id=o.id,
        trade_id=o.trade_id,
        serial_number=o.serial_number,
        ib_order_id=o.ib_order_id,
        leg_type=o.leg_type.value,
        symbol=o.symbol,
        side=o.side,
        security_type=o.security_type.value,
        qty_requested=str(o.qty_requested),
        qty_filled=str(o.qty_filled),
        order_type=o.order_type,
        price_placed=str(o.price_placed) if o.price_placed is not None else None,
        avg_fill_price=str(o.avg_fill_price) if o.avg_fill_price is not None else None,
        commission=str(o.commission) if o.commission is not None else None,
        status=o.status.value,
        placed_at=o.placed_at,
        filled_at=o.filled_at,
    )


@router.get("", response_model=list[OrderResponse])
def list_open_orders(orders: OrderRepository = Depends(get_orders)):
    """List all open (non-terminal) orders."""
    rows = orders.get_all_open()
    return [_serialize_order(o) for o in rows]


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(order_id: str, orders: OrderRepository = Depends(get_orders)):
    """Get a single order by ID."""
    o = orders.get_by_id(order_id)
    if o is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return _serialize_order(o)
