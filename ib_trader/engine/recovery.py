"""Startup crash recovery for in-flight orders.

On REPL startup, scans SQLite for orders in REPRICING, AMENDING, or PENDING
state that are stale (no IB order ID) and marks them ABANDONED.
Also closes trade groups where all order legs are terminal.

Does NOT attempt to cancel or continue — the order may still be open in IB.
Prints a warning listing abandoned orders for the user to handle manually.
"""
import logging
from datetime import datetime

from ib_trader.data.models import OrderStatus, TradeStatus
from ib_trader.data.repository import OrderRepository, TradeRepository

logger = logging.getLogger(__name__)

# Order statuses that indicate the order lifecycle is complete.
_TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.CANCELED,
    OrderStatus.ABANDONED, OrderStatus.CLOSED_MANUAL,
    OrderStatus.CLOSED_EXTERNAL, OrderStatus.REJECTED,
}


def recover_in_flight_orders(orders: OrderRepository) -> list[dict]:
    """Scan for stale in-flight orders and mark them ABANDONED.

    Targets:
      - REPRICING or AMENDING orders (crash mid-reprice)
      - PENDING orders with no ib_order_id (never sent to IB)
      - OPEN orders with no ib_order_id (stale close legs)

    Args:
        orders: OrderRepository instance.

    Returns:
        List of dicts describing each abandoned order, for user display.
        Each dict has: order_id, serial_number, symbol, status, last_amended_at.
    """
    stale_states = [
        OrderStatus.REPRICING, OrderStatus.AMENDING,
        OrderStatus.PENDING, OrderStatus.OPEN,
    ]
    candidates = orders.get_in_states(stale_states)

    abandoned = []
    for order in candidates:
        # PENDING/OPEN orders that have an ib_order_id may still be live in IB.
        # Only abandon those with no ib_order_id (never placed or stale).
        if order.status in (OrderStatus.PENDING, OrderStatus.OPEN) and order.ib_order_id:
            continue

        previous_status = order.status.value
        last_amended = order.last_amended_at
        serial = order.serial_number
        symbol = order.symbol
        order_id = order.id

        logger.warning(
            '{"event": "ORDER_ABANDONED", "order_id": "%s", "serial": %s, '
            '"symbol": "%s", "previous_status": "%s", "last_amended_at": "%s"}',
            order_id, serial, symbol, previous_status,
            last_amended.isoformat() if last_amended else None,
        )
        orders.update_status(order_id, OrderStatus.ABANDONED)
        abandoned.append({
            "order_id": order_id,
            "serial_number": serial,
            "symbol": symbol,
            "previous_status": previous_status,
            "last_amended_at": last_amended,
        })

    return abandoned


def close_orphaned_trade_groups(
    trades: TradeRepository, orders: OrderRepository,
) -> int:
    """Close trade groups where all order legs are terminal.

    A trade group left OPEN after all its legs completed (filled, canceled,
    abandoned, etc.) is an orphan — typically caused by a crash or error
    during order placement.  This function marks them CLOSED.

    Args:
        trades: TradeRepository instance.
        orders: OrderRepository instance.

    Returns:
        Number of trade groups closed.
    """
    open_trades = trades.get_open()
    closed_count = 0
    for trade in open_trades:
        legs = orders.get_for_trade(trade.id)
        if not legs:
            # Trade group with no legs at all — close it.
            trades.update_status(trade.id, TradeStatus.CLOSED)
            closed_count += 1
            logger.info(
                '{"event": "TRADE_GROUP_CLOSED_ORPHAN", "trade_id": "%s", '
                '"serial": %s, "reason": "no order legs"}',
                trade.id, trade.serial_number,
            )
            continue

        all_terminal = all(o.status in _TERMINAL_ORDER_STATUSES for o in legs)
        has_fill = any(o.avg_fill_price is not None for o in legs)

        if all_terminal and not has_fill:
            # All legs are terminal but none filled — failed placement attempt.
            trades.update_status(trade.id, TradeStatus.CLOSED)
            closed_count += 1
            logger.info(
                '{"event": "TRADE_GROUP_CLOSED_ORPHAN", "trade_id": "%s", '
                '"serial": %s, "reason": "all legs terminal, no fills"}',
                trade.id, trade.serial_number,
            )

    return closed_count


def format_recovery_warnings(abandoned: list[dict]) -> list[str]:
    """Format abandoned order info as user-facing warning lines.

    Args:
        abandoned: List of abandoned order dicts from recover_in_flight_orders().

    Returns:
        List of warning strings for display at the REPL prompt.
    """
    lines = []
    for order in abandoned:
        serial = order.get("serial_number", "?")
        symbol = order.get("symbol", "?")
        prev = order.get("previous_status", "?")
        amended_at = order.get("last_amended_at")
        time_str = ""
        if amended_at:
            if isinstance(amended_at, datetime):
                time_str = f" (last amended {amended_at.strftime('%H:%M:%S')} UTC)"
        lines.append(
            f"\u26a0 Warning: Order #{serial} ({symbol}) was ABANDONED — "
            f"was {prev}{time_str}. Check IB manually."
        )
    return lines
