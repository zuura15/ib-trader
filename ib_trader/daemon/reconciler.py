"""IB reconciliation logic for the daemon.

Two reconciliation functions:

1. run_reconciliation (legacy) — reconciles the local orders table with IB.
2. run_transaction_reconciliation — compares non-terminal transactions against
   IB open orders and surfaces discrepancies as WARNINGs. Never auto-heals.

After updating an order, checks whether all legs of the trade group
have reached terminal states.  If so, transitions the TradeGroup to
CLOSED, writes closed_at, and computes realized_pnl when entry and
exit fill prices are both available.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.config.context import AppContext
from ib_trader.data.models import (
    LegType, TradeStatus,
    TransactionAction, TransactionEvent, AlertSeverity, SystemAlert,
)

# Fill actions used for P&L calculation and position tracking
_FILL_ACTIONS = {TransactionAction.FILLED, TransactionAction.PARTIAL_FILL}

logger = logging.getLogger(__name__)

# IB statuses that mean the order is no longer working
FILLED_STATUSES = {"Filled"}
CANCELED_STATUSES = {"Cancelled", "Inactive", "ApiCancelled"}


def _now_utc() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def _maybe_close_trade_group(ctx: AppContext, trade_id: str) -> None:
    """Close the trade group if every leg has reached a terminal state.

    When closed, computes realized_pnl from entry vs exit fill prices
    (accounting for direction) and sums commission across all legs.

    Args:
        ctx: Application dependency injection container.
        trade_id: UUID of the trade group to check.
    """
    leg_summary = ctx.transactions.get_trade_leg_summary(trade_id)
    if not leg_summary:
        return

    # If any leg is still non-terminal, the trade group stays open.
    if any(not t.is_terminal for t in leg_summary):
        return

    # --- compute realized P&L from filled legs ---
    filled_legs = ctx.transactions.get_filled_legs(trade_id)
    all_txns = ctx.transactions.get_for_trade(trade_id)

    entry_value = Decimal("0")
    exit_value = Decimal("0")
    total_commission = Decimal("0")
    has_entry = False
    has_exit = False
    direction = None

    # Sum commission from all transactions
    for t in all_txns:
        commission = t.commission or Decimal("0")
        total_commission += commission

    for t in filled_legs:
        qty = t.ib_filled_qty or Decimal("0")
        price = t.ib_avg_fill_price or Decimal("0")

        if t.leg_type == LegType.ENTRY and t.action in _FILL_ACTIONS:
            entry_value += price * qty
            has_entry = True
            direction = t.side  # BUY for LONG, SELL for SHORT
        elif t.leg_type in (LegType.PROFIT_TAKER, LegType.STOP_LOSS, LegType.CLOSE) and t.action in _FILL_ACTIONS:
            exit_value += price * qty
            has_exit = True

    realized_pnl = None
    if has_entry and has_exit and direction is not None:
        if direction == "BUY":
            # Long trade: profit = exit - entry
            realized_pnl = exit_value - entry_value
        else:
            # Short trade: profit = entry - exit
            realized_pnl = entry_value - exit_value

    if realized_pnl is not None:
        ctx.trades.update_pnl(trade_id, realized_pnl, total_commission)

    ctx.trades.update_status(trade_id, TradeStatus.CLOSED)
    logger.info(
        '{"event": "TRADE_GROUP_CLOSED", "trade_id": "%s", '
        '"realized_pnl": "%s", "total_commission": "%s"}',
        trade_id,
        str(realized_pnl) if realized_pnl is not None else "null",
        str(total_commission),
    )


async def run_reconciliation(ctx: AppContext) -> dict:
    """Query IB for all open orders and reconcile with SQLite.

    Args:
        ctx: Application dependency injection container.

    Returns:
        dict with 'changes' count and 'details' list of changed order IDs.
    """
    logger.info('{"event": "RECONCILIATION_STARTED"}')
    changes = []

    try:
        ib_orders = await ctx.ib.get_open_orders()
        ib_by_id = {o["ib_order_id"]: o for o in ib_orders}

        # Check all locally-tracked open orders (from transactions) against IB
        local_open = ctx.transactions.get_open_orders()

        now = _now_utc()
        for txn in local_open:
            if not txn.ib_order_id:
                continue

            ib_status = ib_by_id.get(txn.ib_order_id)

            if ib_status is None:
                # Order not found in IB's open orders — may have been filled or canceled externally
                full_status = await ctx.ib.get_order_status(txn.ib_order_id)
                ib_str = full_status["status"]
                qty_filled = full_status["qty_filled"]
                avg_price = full_status["avg_fill_price"]
                commission = full_status["commission"] or Decimal("0")

                if ib_str in FILLED_STATUSES:
                    reconciled_event = TransactionEvent(
                        ib_order_id=txn.ib_order_id,
                        ib_perm_id=txn.ib_perm_id,
                        action=TransactionAction.RECONCILED,
                        symbol=txn.symbol,
                        side=txn.side,
                        order_type=txn.order_type,
                        quantity=txn.quantity,
                        limit_price=txn.limit_price,
                        account_id=txn.account_id,
                        ib_status=ib_str,
                        ib_filled_qty=qty_filled,
                        ib_avg_fill_price=avg_price or Decimal("0"),
                        commission=commission,
                        trade_serial=txn.trade_serial,
                        trade_id=txn.trade_id,
                        leg_type=txn.leg_type,
                        requested_at=now,
                        ib_responded_at=now,
                        is_terminal=True,
                        # Epic 1 D14: archival rows self-describing.
                        security_type=getattr(txn, "security_type", None),
                        expiry=getattr(txn, "expiry", None),
                        trading_class=getattr(txn, "trading_class", None),
                        multiplier=getattr(txn, "multiplier", None),
                        con_id=getattr(txn, "con_id", None),
                    )
                    ctx.transactions.insert(reconciled_event)
                    logger.info(
                        '{"event": "RECONCILED_EXTERNAL", "ib_order_id": %d, '
                        '"symbol": "%s", "ib_status": "Filled", '
                        '"qty_filled": "%s"}',
                        txn.ib_order_id, txn.symbol, qty_filled,
                    )
                    changes.append(txn.ib_order_id)
                    if txn.trade_id:
                        _maybe_close_trade_group(ctx, txn.trade_id)

                elif ib_str in CANCELED_STATUSES:
                    reconciled_event = TransactionEvent(
                        ib_order_id=txn.ib_order_id,
                        ib_perm_id=txn.ib_perm_id,
                        action=TransactionAction.RECONCILED,
                        symbol=txn.symbol,
                        side=txn.side,
                        order_type=txn.order_type,
                        quantity=txn.quantity,
                        limit_price=txn.limit_price,
                        account_id=txn.account_id,
                        ib_status=ib_str,
                        trade_serial=txn.trade_serial,
                        trade_id=txn.trade_id,
                        leg_type=txn.leg_type,
                        requested_at=now,
                        ib_responded_at=now,
                        is_terminal=True,
                        security_type=getattr(txn, "security_type", None),
                        expiry=getattr(txn, "expiry", None),
                        trading_class=getattr(txn, "trading_class", None),
                        multiplier=getattr(txn, "multiplier", None),
                        con_id=getattr(txn, "con_id", None),
                    )
                    ctx.transactions.insert(reconciled_event)
                    logger.info(
                        '{"event": "RECONCILED_EXTERNAL", "ib_order_id": %d, '
                        '"symbol": "%s", "ib_status": "Canceled"}',
                        txn.ib_order_id, txn.symbol,
                    )
                    changes.append(txn.ib_order_id)
                    if txn.trade_id:
                        _maybe_close_trade_group(ctx, txn.trade_id)

    except Exception as e:
        logger.error(
            '{"event": "RECONCILIATION_FAILED", "error": "%s"}', str(e), exc_info=True
        )
        return {"changes": 0, "details": [], "error": str(e)}

    result = {"changes": len(changes), "details": changes}
    logger.info(
        '{"event": "RECONCILIATION_COMPLETE", "changes": %d}', len(changes)
    )
    return result


async def run_transaction_reconciliation(ctx: AppContext) -> dict:
    """Compare non-terminal transactions against IB open orders.

    For each order that appears in our transactions (non-terminal) but is
    not found in IB's open orders, writes a RECONCILED row and emits a
    WARNING alert. Does NOT auto-heal — discrepancies are flagged only.

    Args:
        ctx: Application dependency injection container.

    Returns:
        dict with 'discrepancies' count and 'details' list.
    """
    logger.info('{"event": "TRANSACTION_RECONCILIATION_STARTED"}')

    discrepancies = []

    try:
        ib_orders = await ctx.ib.get_open_orders()
        ib_open_ids = {int(o["ib_order_id"]) for o in ib_orders}

        our_open = ctx.transactions.get_open_orders()

        for txn in our_open:
            if txn.ib_order_id is None:
                continue

            if txn.ib_order_id not in ib_open_ids:
                # Discrepancy: our records say open, IB says not open
                now = _now_utc()
                discrepancy_event = TransactionEvent(
                    ib_order_id=txn.ib_order_id,
                    ib_perm_id=txn.ib_perm_id,
                    action=TransactionAction.DISCREPANCY,
                    symbol=txn.symbol,
                    side=txn.side,
                    order_type=txn.order_type,
                    quantity=txn.quantity,
                    limit_price=txn.limit_price,
                    account_id=txn.account_id,
                    ib_status="NOT_FOUND_IN_IB",
                    trade_serial=txn.trade_serial,
                    requested_at=now,
                    ib_responded_at=now,
                    is_terminal=False,
                )
                ctx.transactions.insert(discrepancy_event)

                # Emit WARNING alert
                alert_msg = (
                    f"Order {txn.ib_order_id} ({txn.symbol}) is open in our records "
                    f"but not found in IB — manual reconciliation required"
                )
                alert = SystemAlert(
                    severity=AlertSeverity.WARNING,
                    trigger="TRANSACTION_RECONCILIATION",
                    message=alert_msg,
                    created_at=now,
                )
                ctx.alerts.create(alert)

                logger.warning(
                    '{"event": "TRANSACTION_RECONCILIATION_DISCREPANCY", '
                    '"ib_order_id": %d, "symbol": "%s", "message": "%s"}',
                    txn.ib_order_id, txn.symbol, alert_msg,
                )
                discrepancies.append(txn.ib_order_id)

    except Exception as e:
        logger.error(
            '{"event": "TRANSACTION_RECONCILIATION_FAILED", "error": "%s"}',
            str(e), exc_info=True,
        )
        return {"discrepancies": 0, "details": [], "error": str(e)}

    result = {"discrepancies": len(discrepancies), "details": discrepancies}
    logger.info(
        '{"event": "TRANSACTION_RECONCILIATION_COMPLETE", "discrepancies": %d}',
        len(discrepancies),
    )
    return result
