"""Startup crash recovery for in-flight orders.

On REPL startup, scans SQLite for orders in REPRICING or AMENDING state.
For each: marks as ABANDONED, logs clearly with timestamp and last known step.
Does NOT attempt to cancel or continue — the order may still be open in IB.
Prints a warning listing abandoned orders for the user to handle manually.
"""
import logging
from datetime import datetime

from ib_trader.data.models import OrderStatus
from ib_trader.data.repository import OrderRepository

logger = logging.getLogger(__name__)


def recover_in_flight_orders(orders: OrderRepository) -> list[dict]:
    """Scan for orders in REPRICING or AMENDING state and mark them ABANDONED.

    Args:
        orders: OrderRepository instance.

    Returns:
        List of dicts describing each abandoned order, for user display.
        Each dict has: order_id, serial_number, symbol, status, last_amended_at.
    """
    in_flight_states = [OrderStatus.REPRICING, OrderStatus.AMENDING]
    in_flight = orders.get_in_states(in_flight_states)

    abandoned = []
    for order in in_flight:
        # Capture previous status before update — SQLAlchemy may mutate the object in-session
        previous_status = order.status.value
        last_amended = order.last_amended_at
        serial = order.serial_number
        symbol = order.symbol
        order_id = order.id

        logger.warning(
            '{"event": "ORDER_ABANDONED", "order_id": "%s", "serial": %s, '
            '"symbol": "%s", "previous_status": "%s", "last_amended_at": "%s"}',
            order_id,
            serial,
            symbol,
            previous_status,
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
