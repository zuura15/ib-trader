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
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ib_trader.config.context import AppContext
from ib_trader.data.models import (
    LegType, TradeStatus,
    TransactionAction, TransactionEvent, AlertSeverity, SystemAlert,
)
from ib_trader.logging_.alerts import log_and_alert

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
    # Contract multiplier. Stocks default to 1 (no multiplier). For
    # futures (FUT) the per-point-per-contract value is loaded from
    # any transaction row that carries it — PLACE_ACCEPTED rows
    # always have it set since execute_order writes it at place
    # time. Pre-fix we left this at an implicit 1, which understated
    # FUT realized P&L by the multiplier (e.g. NQ trades at 20× off
    # — observed on the operator's 2026-06-08 closes). See
    # ``_write_txn(multiplier=...)`` callers in engine/order.py.
    multiplier = Decimal("1")
    for t in all_txns:
        if t.multiplier:
            try:
                m = Decimal(str(t.multiplier))
                if m > 0:
                    multiplier = m
                    break
            except (ValueError, ArithmeticError):
                continue

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
        # Apply contract multiplier so FUT realized P&L is in dollars
        # rather than price-points × qty. STK leaves multiplier=1.
        if direction == "BUY":
            # Long trade: profit = (exit - entry) × multiplier
            realized_pnl = (exit_value - entry_value) * multiplier
        else:
            # Short trade: profit = (entry - exit) × multiplier
            realized_pnl = (entry_value - exit_value) * multiplier

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


async def run_cancel_verification(
    ctx: AppContext, *, since_minutes: int = 240,
) -> dict:
    """Detect ledger-cancelled orders that IB still has working.

    Backstop for the 10340 silent-cancel-rejection failure mode (see
    ``engine/order._finalize_partial_cancel``). The at-decision probe
    in the engine handles the live path; this catches anything that
    slipped through (e.g. orders cancelled before this fix shipped, or
    a probe-failure case the engine fail-safed past as a soft warning
    rather than a hard halt).

    For each recently-cancelled order, queries IB's open-orders list.
    If the order is still there, writes a ``TransactionAction.DISCREPANCY``
    row and fires a ``CATASTROPHIC`` alert naming the order_id and qty.
    Does NOT auto-heal — operator must reconcile in TWS.

    Returns ``{checked, still_open, details}``.
    """
    logger.info(
        '{"event": "CANCEL_VERIFICATION_STARTED", "since_minutes": %d}',
        since_minutes,
    )

    still_open: list[int] = []
    try:
        cancelled = ctx.transactions.get_recent_cancelled(
            since_minutes=since_minutes,
        )
        if not cancelled:
            logger.info(
                '{"event": "CANCEL_VERIFICATION_COMPLETE", '
                '"checked": 0, "still_open": 0}',
            )
            return {"checked": 0, "still_open": 0, "details": []}

        ib_orders = await ctx.ib.get_open_orders()
        ib_open_ids = {int(o["ib_order_id"]) for o in ib_orders}

        for txn in cancelled:
            if txn.ib_order_id is None:
                continue
            if txn.ib_order_id not in ib_open_ids:
                continue

            # Ledger says CANCELLED but IB still has it open. This is
            # the 10340 silent-rejection signature, or any other path
            # where the at-decision probe missed.
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
                ib_status="LEDGER_CANCELLED_BUT_LIVE_AT_IB",
                trade_serial=txn.trade_serial,
                requested_at=now,
                ib_responded_at=now,
                is_terminal=False,
            )
            ctx.transactions.insert(discrepancy_event)

            alert_msg = (
                f"Order #{txn.ib_order_id} ({txn.symbol} {txn.side} "
                f"{txn.quantity}) is recorded CANCELLED in the audit ledger "
                f"but IB still shows it as open. Verify and cancel in TWS — "
                f"this is the silent-cancel-rejection pattern (IB error 10340)."
            )
            alert = SystemAlert(
                severity=AlertSeverity.CATASTROPHIC,
                trigger="CANCEL_VERIFICATION_DISCREPANCY",
                message=alert_msg,
                created_at=now,
            )
            ctx.alerts.create(alert)

            logger.error(
                '{"event": "CANCEL_VERIFICATION_DISCREPANCY", '
                '"ib_order_id": %d, "symbol": "%s", "qty": "%s"}',
                txn.ib_order_id, txn.symbol, str(txn.quantity),
            )
            still_open.append(txn.ib_order_id)

    except Exception as e:
        logger.error(
            '{"event": "CANCEL_VERIFICATION_FAILED", "error": "%s"}',
            str(e), exc_info=True,
        )
        return {
            "checked": 0, "still_open": 0,
            "details": [], "error": str(e),
        }

    logger.info(
        '{"event": "CANCEL_VERIFICATION_COMPLETE", '
        '"checked": %d, "still_open": %d}',
        len(cancelled), len(still_open),
    )
    return {
        "checked": len(cancelled),
        "still_open": len(still_open),
        "details": still_open,
    }


async def run_commission_reconciliation(
    ctx: AppContext, lookback_hours: float = 24.0,
) -> dict:
    """Sweep bot_trades for rows whose stored commission is below the
    expected per-symbol round-trip floor and backfill them from
    ``transactions.commission`` (time-bounded ±1h around exit_time so
    stale serials can't contaminate).

    Closes the race where IB's ``commissionReport`` lands BEFORE the
    bot_trade row exists — in that window ``add_commission_by_serial``
    matches 0 rows and the commission is effectively lost. The
    forward-fix (``runtime._handle_record_trade_closed`` seeds from
    transactions at creation) handles the common case; this is the
    safety net.

    Stragglers older than 24h that STILL undercount are surfaced via
    ``log_and_alert(trigger="COMMISSION_DELIVERY_GAP", severity="WARNING")``
    so the upstream ``_on_commission_report`` bug shows up instead of
    being silently papered over.

    Skips bots currently in an active FSM state (per the bot's Redis
    doc) so we don't race the live commission backfill.
    """
    logger.info('{"event": "COMMISSION_RECONCILIATION_STARTED"}')

    if ctx.bot_trades is None or ctx.redis is None:
        logger.info('{"event": "COMMISSION_RECONCILIATION_SKIPPED",'
                    ' "reason": "bot_trades or redis unavailable"}')
        return {"backfilled": 0, "warned": 0, "details": []}

    # Build the active-bot skip set from Redis. Best-effort: if Redis
    # is unreachable, run the sweep without the skip filter.
    from ib_trader.bots.lifecycle import ACTIVE_STATES, bot_doc_key
    from ib_trader.redis.state import StateStore
    skip_bot_ids: set[str] = set()
    try:
        store = StateStore(ctx.redis)
        candidates_for_skip = ctx.bot_trades.find_undercommissioned_trades(
            lookback_hours,
        )
        # Probe each candidate's bot doc once; cheap and bounded by
        # candidate count.
        seen: set[str] = set()
        for row in candidates_for_skip:
            if row.bot_id in seen:
                continue
            seen.add(row.bot_id)
            doc = await store.get(bot_doc_key(row.bot_id)) or {}
            state_str = str(doc.get("state", ""))
            if any(state_str == s.value for s in ACTIVE_STATES):
                skip_bot_ids.add(row.bot_id)
    except Exception:
        logger.debug("commission reconciler skip-set probe failed",
                     exc_info=True)

    candidates = ctx.bot_trades.find_undercommissioned_trades(
        lookback_hours, skip_bot_ids=skip_bot_ids,
    )

    backfilled: list[dict] = []
    warned: list[dict] = []
    now = _now_utc()

    for row in candidates:
        # Time-bounded sum from transactions for both legs. Mirrors the
        # SQL backfill we ran one-shot — ±1h around exit_time keeps
        # stale serial references from poisoning the result.
        # `row.exit_time` is UTC-naive (server-local stored as UTC);
        # treat as aware UTC for the comparison.
        exit_t = row.exit_time
        if exit_t is None:
            continue
        if exit_t.tzinfo is None:
            exit_t = exit_t.replace(tzinfo=timezone.utc)
        from sqlalchemy import func, or_, and_
        s = ctx.transactions._session()
        lo = exit_t - timedelta(hours=1)
        hi = exit_t + timedelta(hours=1)
        serials = [x for x in (row.entry_serial, row.exit_serial)
                   if x is not None]
        if not serials:
            continue
        result = (
            s.query(func.coalesce(
                func.sum(TransactionEvent.commission), 0,
            ))
            .filter(
                TransactionEvent.trade_serial.in_(serials),
                TransactionEvent.action.in_([
                    TransactionAction.FILLED,
                    TransactionAction.PARTIAL_FILL,
                ]),
                TransactionEvent.requested_at >= lo,
                TransactionEvent.requested_at <= hi,
            )
            .scalar()
        )
        summed = Decimal(str(result)) if result is not None else Decimal("0")
        old_commission = row.commission or Decimal("0")

        if summed > old_commission:
            wrote = ctx.bot_trades.update_commission_if_higher(
                row.id, summed,
            )
            if wrote:
                logger.info(
                    '{"event": "BOT_TRADE_COMMISSION_BACKFILLED", '
                    '"trade_id": "%s", "symbol": "%s", '
                    '"old_commission": "%s", "new_commission": "%s", '
                    '"source": "transactions_sweep"}',
                    row.id, row.symbol, str(old_commission), str(summed),
                )
                backfilled.append({
                    "trade_id": row.id,
                    "symbol": row.symbol,
                    "old_commission": str(old_commission),
                    "new_commission": str(summed),
                })
                # Recheck: if still below floor, fall through to warn.
                if summed < _floor_for(row.symbol):
                    await _warn_delivery_gap(ctx, row, summed, now, warned)
            else:
                # Concurrent write won — that's fine.
                continue
        elif exit_t < now - timedelta(hours=24):
            await _warn_delivery_gap(ctx, row, old_commission, now, warned)

    result = {
        "backfilled": len(backfilled),
        "warned": len(warned),
        "details": {"backfilled": backfilled, "warned": warned},
    }
    logger.info(
        '{"event": "COMMISSION_RECONCILIATION_COMPLETE", '
        '"backfilled": %d, "warned": %d}',
        len(backfilled), len(warned),
    )
    return result


def _floor_for(symbol: str) -> Decimal:
    from ib_trader.data.commissions import expected_min
    return expected_min(symbol)


async def _warn_delivery_gap(
    ctx: AppContext, row, current_commission: Decimal,
    now: datetime, warned: list[dict],
) -> None:
    """Emit a WARNING surfacing the upstream commission-delivery bug.
    Only called for rows whose ``exit_time`` is older than 24h AND
    whose commission is STILL below the symbol floor — anything fresher
    might just be waiting on a ``commissionReport`` that hasn't landed
    yet, and we don't want to fire false alarms during normal latency
    windows.
    """
    floor = _floor_for(row.symbol)
    msg = (
        f"bot_trade {row.id[:8]} ({row.symbol}) commission "
        f"${current_commission} is below the expected floor "
        f"${floor} more than 24h after exit — the IB "
        f"commissionReport for this round-trip was never applied. "
        f"Investigate _on_commission_report dispatch / dedup."
    )
    await log_and_alert(
        redis=ctx.redis,
        trigger="COMMISSION_DELIVERY_GAP",
        message=msg,
        severity="WARNING",
        bot_id=row.bot_id,
        symbol=row.symbol,
        extra={
            "trade_id": row.id,
            "stored_commission": str(current_commission),
            "expected_floor": str(floor),
            "exit_time": (
                row.exit_time.isoformat() if row.exit_time else None
            ),
        },
        exc_info=False,
    )
    warned.append({
        "trade_id": row.id,
        "symbol": row.symbol,
        "stored_commission": str(current_commission),
        "expected_floor": str(floor),
    })
