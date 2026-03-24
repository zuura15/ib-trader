"""Startup crash recovery for in-flight orders.

On REPL startup, scans SQLite for trades with PLACE_ATTEMPT but no
PLACE_ACCEPTED and no terminal event, and marks them CLOSED.
Also closes trade groups where all transaction legs are terminal with no fills.

Does NOT attempt to cancel or continue — the order may still be open in IB.
Prints a warning listing abandoned trades for the user to handle manually.
"""
import logging

from ib_trader.data.models import TradeStatus, TransactionAction
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repository import TradeRepository

logger = logging.getLogger(__name__)


def recover_in_flight_orders(
    transactions: TransactionRepository, trades: TradeRepository,
) -> list[dict]:
    """Find trades with PLACE_ATTEMPT but no PLACE_ACCEPTED and no terminal event.

    These are orders that crashed mid-placement — they may or may not have
    reached IB. Mark the trade groups CLOSED and return warnings.

    Args:
        transactions: TransactionRepository instance.
        trades: TradeRepository instance.

    Returns:
        List of dicts describing each abandoned trade, for user display.
        Each dict has: trade_id, serial_number, symbol.
    """
    open_trades = trades.get_open()
    abandoned = []

    for trade in open_trades:
        if not transactions.has_unconfirmed_placements(trade.id):
            continue

        # This trade has unconfirmed placements — mark it CLOSED.
        logger.warning(
            '{"event": "TRADE_ABANDONED", "trade_id": "%s", "serial": %s, '
            '"symbol": "%s"}',
            trade.id, trade.serial_number, trade.symbol,
        )
        trades.update_status(trade.id, TradeStatus.CLOSED)
        abandoned.append({
            "trade_id": trade.id,
            "serial_number": trade.serial_number,
            "symbol": trade.symbol,
        })

    return abandoned


def close_orphaned_trade_groups(
    trades: TradeRepository, transactions: TransactionRepository,
) -> int:
    """Close trade groups where all transaction legs are terminal.

    A trade group left OPEN after all its legs completed (filled, canceled,
    etc.) is an orphan — typically caused by a crash or error during order
    placement. This function marks them CLOSED.

    For each OPEN trade, uses get_trade_leg_summary to check if all legs
    are terminal. If yes and no fills, closes the trade.

    Args:
        trades: TradeRepository instance.
        transactions: TransactionRepository instance.

    Returns:
        Number of trade groups closed.
    """
    open_trades = trades.get_open()
    closed_count = 0
    for trade in open_trades:
        legs = transactions.get_trade_leg_summary(trade.id)
        if not legs:
            # Trade group with no transaction legs at all — close it.
            trades.update_status(trade.id, TradeStatus.CLOSED)
            closed_count += 1
            logger.info(
                '{"event": "TRADE_GROUP_CLOSED_ORPHAN", "trade_id": "%s", '
                '"serial": %s, "reason": "no transaction legs"}',
                trade.id, trade.serial_number,
            )
            continue

        all_terminal = all(t.is_terminal for t in legs)
        has_fill = any(
            t.action in (TransactionAction.FILLED, TransactionAction.PARTIAL_FILL)
            for t in legs
        )

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
    """Format abandoned trade info as user-facing warning lines.

    Args:
        abandoned: List of abandoned trade dicts from recover_in_flight_orders().

    Returns:
        List of warning strings for display at the REPL prompt.
    """
    lines = []
    for trade in abandoned:
        serial = trade.get("serial_number", "?")
        symbol = trade.get("symbol", "?")
        lines.append(
            f"\u26a0 Warning: Trade #{serial} ({symbol}) had unconfirmed placements — "
            f"marked CLOSED. Check IB manually."
        )
    return lines
