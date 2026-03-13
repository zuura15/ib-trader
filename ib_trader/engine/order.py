"""Core order execution logic.

execute_order: places and manages an entry order (buy or sell).
reprice_loop: manages the reprice loop for mid-price orders.
place_profit_taker: places a GTC profit taker after fill.

All engine functions receive AppContext and call IB exclusively through ctx.ib.
No engine function imports or references ib_insync directly.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.config.context import AppContext
from ib_trader.repl.output_router import OutputPane, OutputSeverity
from ib_trader.data.models import (
    LegType, Order, OrderStatus, RepriceEvent, SecurityType, TradeGroup, TradeStatus,
    TransactionAction, TransactionEvent,
)
from ib_trader.engine.exceptions import IBOrderRejectedError, SafetyLimitError, TradeNotFoundError
from ib_trader.engine.market_hours import (
    is_ib_session_active, is_overnight_session, presubmitted_reason, session_label,
)


def _session_tif() -> str:
    """Return the correct TIF for the current market session.

    Always returns GTC.  During the overnight session (8 PM – 3:50 AM ET),
    insync_client.place_limit_order routes to ``exchange=OVERNIGHT``.
    TIF stays GTC — the OND TIF is Web API only and does not work on the
    TWS socket API (error 10052).
    """
    return "GTC"
from ib_trader.engine.pricing import (
    calc_mid, calc_profit_taker_price, calc_profit_taker_price_short, calc_step_price,
    calc_shares_from_dollars,
)
from ib_trader.repl.commands import BuyCommand, SellCommand, CloseCommand, Strategy

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_int(val) -> int | None:
    """Convert a value to int, returning None if not convertible."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _write_txn(
    ctx: AppContext,
    action: TransactionAction,
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    limit_price: Decimal | None = None,
    ib_order_id: int | None = None,
    ib_perm_id: int | None = None,
    ib_status: str | None = None,
    ib_filled_qty: Decimal | None = None,
    ib_avg_fill_price: Decimal | None = None,
    ib_error_code: int | None = None,
    ib_error_message: str | None = None,
    trade_serial: int | None = None,
    is_terminal: bool = False,
    ib_responded_at: datetime | None = None,
) -> None:
    """Write a single TransactionEvent row to the audit log.

    No-op if ctx.transactions is None (backward-compatible with tests
    that don't set up the transactions repository).
    """
    if ctx.transactions is None:
        return
    now = _now_utc()
    event = TransactionEvent(
        ib_order_id=ib_order_id,
        ib_perm_id=ib_perm_id,
        action=action,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        account_id=ctx.account_id,
        ib_status=ib_status,
        ib_filled_qty=ib_filled_qty,
        ib_avg_fill_price=ib_avg_fill_price,
        ib_error_code=ib_error_code,
        ib_error_message=ib_error_message,
        trade_serial=trade_serial,
        requested_at=now,
        ib_responded_at=ib_responded_at,
        is_terminal=is_terminal,
    )
    try:
        ctx.transactions.insert(event)
    except Exception as exc:
        logger.error(
            '{"event": "TRANSACTION_WRITE_FAILED", "action": "%s", "error": "%s"}',
            action.value, str(exc), exc_info=True,
        )


async def execute_order(
    cmd: "BuyCommand | SellCommand",
    ctx: AppContext,
) -> None:
    """Place and manage an entry order (buy or sell).

    Sequence:
    1. Validate safety limits.
    2. Qualify contract (cache-first).
    3. Create TradeGroup + Order in DB (status=PENDING).
    4. Fetch bid/ask snapshot.
    5. Place IB order.
    6. Write ib_order_id to DB immediately.
    7. Update order status → OPEN/REPRICING.
    8. Register fill/status callbacks.
    9. Start reprice_loop task if strategy == 'mid'.
    10. Await fill event or timeout.
    11. On fill: write fill details, place profit taker if configured.
    12. On timeout: cancel order, write final status.

    Args:
        cmd: Parsed BuyCommand or SellCommand.
        ctx: Application dependency injection container.
    """
    settings = ctx.settings
    side = "BUY" if hasattr(cmd, "__class__") and cmd.__class__.__name__ == "BuyCommand" else "SELL"
    # Determine side from command type
    from ib_trader.repl.commands import BuyCommand as BC
    side = "BUY" if isinstance(cmd, BC) else "SELL"

    # 1. Resolve quantity
    qty = cmd.qty
    contract_info = await _get_contract(cmd.symbol, ctx)
    con_id = contract_info["con_id"]

    if cmd.dollars is not None:
        snapshot = await ctx.ib.get_market_snapshot(con_id)
        mid = calc_mid(snapshot["bid"], snapshot["ask"])
        qty = calc_shares_from_dollars(
            cmd.dollars, mid, settings["max_order_size_shares"]
        )
        if qty == 0:
            ctx.router.emit(
                "\u2717 Error: dollar amount too small for current price",
                pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
            )
            return

    if qty is None or qty <= 0:
        ctx.router.emit(
            "\u2717 Error: quantity must be a positive number",
            pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
        )
        return

    # 2. Validate safety limits
    if qty > settings["max_order_size_shares"]:
        logger.warning(
            '{"event": "SAFETY_LIMIT_EXCEEDED", "symbol": "%s", "qty": "%s", "limit": %d}',
            cmd.symbol, qty, settings["max_order_size_shares"],
        )
        raise SafetyLimitError(
            f"Order size {qty} exceeds max_order_size_shares={settings['max_order_size_shares']}"
        )

    # 3. Create TradeGroup + Order in DB
    serial = ctx.trades.next_serial_number()
    direction = "LONG" if side == "BUY" else "SHORT"
    trade_group = TradeGroup(
        serial_number=serial,
        symbol=cmd.symbol,
        direction=direction,
        status=TradeStatus.OPEN,
        opened_at=_now_utc(),
    )
    trade_group = ctx.trades.create(trade_group)

    order = Order(
        trade_id=trade_group.id,
        serial_number=serial,
        leg_type=LegType.ENTRY,
        symbol=cmd.symbol,
        side=side,
        security_type=SecurityType.STK,
        qty_requested=qty,
        qty_filled=Decimal("0"),
        order_type=cmd.strategy.upper(),
        profit_taker_amount=cmd.profit_amount,
        profit_taker_price=cmd.take_profit_price,
        stop_loss_requested=cmd.stop_loss,
        status=OrderStatus.PENDING,
        placed_at=_now_utc(),
    )
    order = ctx.orders.create(order)

    if cmd.stop_loss:
        logger.info(
            '{"event": "STOP_LOSS_STUB_RECEIVED", "order_id": "%s", "value": "%s"}',
            order.id, cmd.stop_loss,
        )

    logger.info(
        '{"event": "ORDER_CREATED", "trade_id": "%s", "serial": %d, "symbol": "%s", '
        '"side": "%s", "qty": "%s", "strategy": "%s"}',
        trade_group.id, serial, cmd.symbol, side, qty, cmd.strategy,
    )

    try:
        if cmd.strategy == Strategy.LIMIT:
            await _execute_limit_order(cmd, order, trade_group, con_id, side, qty, ctx)
        elif cmd.strategy == Strategy.MID:
            await _execute_mid_order(cmd, order, trade_group, con_id, side, qty, ctx)
        elif cmd.strategy in (Strategy.BID, Strategy.ASK):
            await _execute_bid_ask_order(cmd, order, trade_group, con_id, side, qty, ctx)
        else:
            await _execute_market_order(cmd, order, trade_group, con_id, side, qty, ctx)
    except Exception as e:
        logger.error(
            '{"event": "ORDER_ERROR", "order_id": "%s", "error": "%s"}',
            order.id, str(e), exc_info=True,
        )
        raise


async def _get_contract(symbol: str, ctx: AppContext) -> dict:
    """Get contract details, using cache if fresh."""
    ttl = ctx.settings["cache_ttl_seconds"]
    if ctx.contracts.is_fresh(symbol, ttl):
        cached = ctx.contracts.get(symbol)
        # If the IB client's in-memory contract cache lost this entry (e.g. after
        # a restart), re-qualify to repopulate it.  Without a fully-specified
        # Contract object (symbol + secType + exchange + currency), order placement
        # falls back to a bare Contract(conId=...) which IB silently ignores —
        # the order stays PendingSubmit indefinitely and never fills.
        if not ctx.ib.has_contract_cached(cached.con_id):
            logger.debug('{"event": "CONTRACT_IB_CACHE_MISS", "symbol": "%s"}', symbol)
            await ctx.ib.qualify_contract(symbol)
        else:
            logger.debug('{"event": "CONTRACT_CACHE_HIT", "symbol": "%s"}', symbol)
        return {
            "con_id": cached.con_id,
            "exchange": cached.exchange,
            "currency": cached.currency,
            "multiplier": cached.multiplier,
        }

    logger.debug('{"event": "CONTRACT_CACHE_MISS", "symbol": "%s"}', symbol)
    info = await ctx.ib.qualify_contract(symbol)

    from ib_trader.data.models import Contract
    contract = Contract(
        symbol=symbol,
        con_id=info["con_id"],
        exchange=info["exchange"],
        currency=info["currency"],
        multiplier=info["multiplier"],
        raw_response=info["raw"],
        fetched_at=_now_utc(),
    )
    ctx.contracts.upsert(contract)
    return info


async def _execute_limit_order(
    cmd, order: Order, trade_group: TradeGroup, con_id: int,
    side: str, qty: Decimal, ctx: AppContext,
) -> None:
    """Place a fire-and-forget limit order at a user-specified price.

    The order is placed as GTC (or DAY+includeOvernight during overnight).
    Once IB confirms acceptance (Submitted/PreSubmitted), control returns
    immediately. The order sits in IB indefinitely — fills are captured by
    the existing fill callbacks and daemon reconciliation, even across
    app restarts.

    No reprice loop. No timeout-and-cancel behavior.
    """
    price = cmd.limit_price

    ctx.router.emit(
        f"Order #{trade_group.serial_number} — {side} {qty} {cmd.symbol} @ limit ${price}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
        event="ORDER_PLACED_LIMIT",
    )

    _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "LIMIT",
               qty, limit_price=price, trade_serial=trade_group.serial_number)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, price, outside_rth=True, tif=_session_tif()
    )

    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
               qty, limit_price=price, ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, ib_responded_at=_now_utc())

    ctx.orders.update_ib_order_id(order.id, ib_order_id)
    ctx.orders.update_amended(order.id, price)
    ctx.orders.update_status(order.id, OrderStatus.OPEN)
    ctx.orders.set_raw_response(order.id, json.dumps({
        "ib_order_id": ib_order_id, "price": str(price), "strategy": "limit",
    }))

    # Register fill/status callbacks so SQLite gets updated on fill
    track = ctx.tracker.register(order.id, ib_order_id, cmd.symbol)

    async def on_fill(fill_ib_id: str, _qty: Decimal, _avg: Decimal, commission: Decimal):
        if fill_ib_id == ib_order_id:
            ctx.tracker.notify_filled(fill_ib_id)

    async def on_status(status_ib_id: str, status: str):
        if status_ib_id == ib_order_id and status in ("Cancelled", "Inactive"):
            ctx.tracker.notify_canceled(status_ib_id)

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)
    ctx.ib.register_status_callback(on_status, ib_order_id=ib_order_id)

    # Wait briefly for IB to acknowledge (transition out of PendingSubmit)
    _SUBMIT_POLL_INTERVAL = 0.5
    _SUBMIT_POLL_STEPS = 20  # 10s max
    _pending_statuses = {"", "PendingSubmit"}
    _ib_rejection_reason: str | None = None

    for _ in range(_SUBMIT_POLL_STEPS):
        _st = await ctx.ib.get_order_status(ib_order_id)
        _ib_status = _st["status"]
        _ib_rejection_reason = ctx.ib.get_order_error(ib_order_id)
        if _ib_rejection_reason and _ib_status in _pending_statuses:
            break
        if _ib_status not in _pending_statuses:
            break
        await asyncio.sleep(_SUBMIT_POLL_INTERVAL)
    else:
        _st = await ctx.ib.get_order_status(ib_order_id)
        _ib_status = _st["status"]
        _ib_rejection_reason = ctx.ib.get_order_error(ib_order_id)

    # Handle rejection / failure to acknowledge
    if _ib_status in _pending_statuses or _ib_status in ("Cancelled", "Inactive"):
        await ctx.ib.cancel_order(ib_order_id)
        ctx.orders.update_status(order.id, OrderStatus.ABANDONED)
        ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
        ctx.tracker.unregister(ib_order_id)
        if _ib_rejection_reason:
            reason = _ib_rejection_reason
        elif _ib_status in ("Cancelled", "Inactive"):
            reason = f"IB cancelled order immediately (status: {_ib_status!r})"
        else:
            reason = (
                f"IB did not acknowledge order {ib_order_id} within "
                f"{_SUBMIT_POLL_STEPS * _SUBMIT_POLL_INTERVAL:.0f}s "
                f"(final status: {_ib_status!r})"
            )
        _write_txn(ctx, TransactionAction.PLACE_REJECTED, cmd.symbol, side, "LIMIT",
                   qty, limit_price=price, ib_order_id=_safe_int(ib_order_id),
                   ib_status=_ib_status, ib_error_message=reason,
                   trade_serial=trade_group.serial_number, is_terminal=True,
                   ib_responded_at=_now_utc())
        logger.error(
            '{"event": "LIMIT_ORDER_REJECTED", "order_id": "%s", '
            '"serial": %d, "ib_order_id": "%s", "reason": "%s"}',
            order.id, trade_group.serial_number, ib_order_id, reason,
        )
        raise IBOrderRejectedError(reason)

    # Check for immediate fill (aggressive limit that crossed the spread)
    status = await ctx.ib.get_order_status(ib_order_id)
    qty_filled = status["qty_filled"]
    avg_price = status["avg_fill_price"]
    commission = status["commission"] or Decimal("0")

    if qty_filled > 0 and avg_price is not None and qty_filled >= qty:
        # Fully filled immediately — handle like any other fill
        await _handle_fill(order, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
        ctx.tracker.unregister(ib_order_id)
        return

    # Order is live in IB — fire and forget.
    # Fills are handled by the registered callbacks and daemon reconciliation.
    ctx.router.emit(
        f"● LIMIT ORDER LIVE: {side} {qty} {cmd.symbol} @ ${price} — "
        f"GTC order active in IB\n"
        f"  Serial: #{trade_group.serial_number}  |  IB ID: {ib_order_id}\n"
        f"  Order will persist until filled or manually cancelled.",
        pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
        event="LIMIT_ORDER_LIVE_DISPLAY",
    )
    logger.info(
        '{"event": "LIMIT_ORDER_LIVE", "order_id": "%s", "serial": %d, '
        '"symbol": "%s", "price": "%s", "ib_order_id": "%s"}',
        order.id, trade_group.serial_number, cmd.symbol, price, ib_order_id,
    )

    # Don't unregister tracker — callbacks stay active for the app session.
    # Daemon reconciliation handles fills that occur after app restart.


async def _execute_mid_order(
    cmd, order: Order, trade_group: TradeGroup, con_id: int,
    side: str, qty: Decimal, ctx: AppContext,
) -> None:
    """Place a mid-price limit order with reprice loop."""
    settings = ctx.settings
    total_steps = int(
        settings["reprice_duration_seconds"] / settings["reprice_interval_seconds"]
    )

    snapshot = await ctx.ib.get_market_snapshot(con_id)
    bid, ask, last = snapshot["bid"], snapshot["ask"], snapshot["last"]

    if bid == 0 and ask == 0:
        if last == 0:
            raise ValueError(
                f"Cannot place order for {cmd.symbol}: no market data available "
                "(bid=0, ask=0, last=0). Check market data subscription."
            )
        logger.warning(
            '{"event": "NO_BID_ASK_USING_LAST", "symbol": "%s", "last": "%s", '
            '"reason": "bid/ask unavailable, market likely closed"}',
            cmd.symbol, last,
        )
        bid = ask = last

    mid = calc_mid(bid, ask)

    ctx.router.emit(
        f"Order #{trade_group.serial_number} \u2014 {side} {qty} {cmd.symbol} @ mid\n"
        f"[{_now_utc().strftime('%H:%M:%S')}] Placed @ ${mid} "
        f"(bid: ${bid} ask: ${ask})",
        pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
        event="ORDER_PLACED_MID",
    )

    # PLACE_ATTEMPT before IB call
    _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "LIMIT",
               qty, limit_price=mid, trade_serial=trade_group.serial_number)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, mid, outside_rth=True, tif=_session_tif()
    )

    # PLACE_ACCEPTED — IB returned an order ID
    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
               qty, limit_price=mid, ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, ib_responded_at=_now_utc())

    # Write ib_order_id immediately — critical for crash recovery
    ctx.orders.update_ib_order_id(order.id, ib_order_id)
    ctx.orders.update_status(order.id, OrderStatus.REPRICING)
    ctx.orders.set_raw_response(order.id, json.dumps({"ib_order_id": ib_order_id, "initial_price": str(mid)}))

    # Register in tracker
    track = ctx.tracker.register(order.id, ib_order_id, cmd.symbol)

    # Capture fill details from the execDetailsEvent callback.  IB delivers
    # commission reports asynchronously — they may not appear in
    # get_order_status()'s trade.fills by the time we check after the reprice
    # loop.  Storing them here ensures the FILLED printout shows the real value.
    _fill_commission: Decimal | None = None

    async def on_fill(fill_ib_id: str, _qty: Decimal, _avg: Decimal, commission: Decimal):
        nonlocal _fill_commission
        if fill_ib_id == ib_order_id:
            _fill_commission = commission
            ctx.tracker.notify_filled(fill_ib_id)

    async def on_status(status_ib_id: str, status: str):
        if status_ib_id == ib_order_id and status in ("Cancelled", "Inactive"):
            ctx.tracker.notify_canceled(status_ib_id)

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)
    ctx.ib.register_status_callback(on_status, ib_order_id=ib_order_id)

    # Wait for IB to acknowledge the order (transition out of PendingSubmit).
    # Without this, the reprice loop fires before IB has assigned a permId,
    # causing error 103 (duplicate order id) on the first amendment.
    # Poll every 0.5 s for up to 10 s.
    _SUBMIT_POLL_INTERVAL = 0.5
    _SUBMIT_POLL_STEPS = 20  # 20 * 0.5 s = 10 s max
    _pending_statuses = {"", "PendingSubmit"}
    _ib_rejection_reason: str | None = None
    for _ in range(_SUBMIT_POLL_STEPS):
        _st = await ctx.ib.get_order_status(ib_order_id)
        _ib_status = _st["status"]
        # Check for an IB error captured by the errorEvent callback (e.g. error
        # 110 "price does not conform to minimum price variation", 201 "order
        # rejected").  If present and order is still pending, fail fast with the
        # real IB message instead of waiting out the full timeout.
        _ib_rejection_reason = ctx.ib.get_order_error(ib_order_id)
        if _ib_rejection_reason and _ib_status in _pending_statuses:
            break
        if _ib_status not in _pending_statuses:
            break
        await asyncio.sleep(_SUBMIT_POLL_INTERVAL)
    else:
        _st = await ctx.ib.get_order_status(ib_order_id)
        _ib_status = _st["status"]
        _ib_rejection_reason = ctx.ib.get_order_error(ib_order_id)

    if _ib_status in _pending_statuses or _ib_status in ("Cancelled", "Inactive"):
        # Timed out waiting for acknowledgment, or IB rejected immediately.
        await ctx.ib.cancel_order(ib_order_id)
        ctx.orders.update_status(order.id, OrderStatus.ABANDONED)
        ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
        ctx.tracker.unregister(ib_order_id)
        if _ib_rejection_reason:
            reason = _ib_rejection_reason
        elif _ib_status in ("Cancelled", "Inactive"):
            reason = f"IB cancelled order immediately (status: {_ib_status!r})"
        else:
            reason = (
                f"IB did not acknowledge order {ib_order_id} within "
                f"{_SUBMIT_POLL_STEPS * _SUBMIT_POLL_INTERVAL:.0f}s "
                f"(final status: {_ib_status!r})"
            )
        _write_txn(ctx, TransactionAction.PLACE_REJECTED, cmd.symbol, side, "LIMIT",
                   qty, limit_price=mid, ib_order_id=_safe_int(ib_order_id),
                   ib_status=_ib_status, ib_error_message=reason,
                   trade_serial=trade_group.serial_number, is_terminal=True,
                   ib_responded_at=_now_utc())
        logger.error(
            '{"event": "ORDER_PLACEMENT_FAILED", "order_id": "%s", '
            '"serial": %d, "ib_order_id": "%s", "ib_status": "%s", "reason": "%s"}',
            order.id, trade_group.serial_number, ib_order_id, _ib_status, reason,
        )
        raise IBOrderRejectedError(reason)

    if _ib_status == "PreSubmitted":
        _why_held = _st.get("why_held") or ""
        _expected_reason = presubmitted_reason()
        if not is_ib_session_active():
            # Weekend closure or 3:50-4:00 AM break — PreSubmitted is expected.
            # Leave the GTC order in IB; daemon reconciler catches the fill.
            ctx.orders.update_status(order.id, OrderStatus.OPEN)
            ctx.router.emit(
                f"\u26a0 QUEUED: {side} {qty} {cmd.symbol} @ ${mid} \u2014 "
                f"{_expected_reason}\n"
                f"  GTC order held by IB. No repricing will run.\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                event="ORDER_QUEUED_DISPLAY",
            )
            logger.info(
                '{"event": "ORDER_QUEUED", "order_id": "%s", "serial": %d, '
                '"symbol": "%s", "price": "%s", "reason": "%s"}',
                order.id, trade_group.serial_number, cmd.symbol, mid, _expected_reason,
            )
            ctx.tracker.unregister(ib_order_id)
            return
        else:
            # Active session — order should be Submitted at the exchange.
            # PreSubmitted here means IB is NOT routing it: investigate.
            _detail = f" (IB whyHeld: {_why_held!r})" if _why_held else ""
            reason = (
                f"Order not working at exchange during {session_label()}"
                f"{_detail}. IB accepted but did not route."
            )
            await ctx.ib.cancel_order(ib_order_id)
            ctx.orders.update_status(order.id, OrderStatus.ABANDONED)
            ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
            ctx.tracker.unregister(ib_order_id)
            ctx.router.emit(
                f"\u2717 NOT ACTIVE: {side} {qty} {cmd.symbol} \u2014 {reason}\n"
                f"  Check IB Gateway logs and account permissions.\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
                event="ORDER_PRESUBMITTED_UNEXPECTED_DISPLAY",
            )
            logger.error(
                '{"event": "ORDER_NOT_ROUTED", "order_id": "%s", "serial": %d, '
                '"symbol": "%s", "ib_status": "PreSubmitted", "why_held": "%s"}',
                order.id, trade_group.serial_number, cmd.symbol, _why_held,
            )
            return  # error already emitted; order marked ABANDONED above

    # Start reprice loop
    reprice_task = asyncio.create_task(
        reprice_loop(
            order_id=order.id,
            ib_order_id=ib_order_id,
            con_id=con_id,
            symbol=cmd.symbol,
            side=side,
            ctx=ctx,
            total_steps=total_steps,
            interval_seconds=float(settings["reprice_interval_seconds"]),
            initial_price=mid,
        )
    )

    # Await fill or timeout
    total_duration = float(settings["reprice_duration_seconds"])
    try:
        await asyncio.wait_for(track.fill_event.wait(), timeout=total_duration + 2)
    except asyncio.TimeoutError:
        pass

    reprice_task.cancel()
    try:
        await reprice_task
    except asyncio.CancelledError:
        pass

    # Determine outcome.
    # Use concrete fill data from IB rather than track.is_filled alone:
    # track.is_filled can be set before orderStatus.filled is updated by IB,
    # producing "FILLED: 0.0 shares @ $None" if we trust the event flag without
    # verifying the actual quantities.
    status = await ctx.ib.get_order_status(ib_order_id)
    qty_filled = status["qty_filled"]
    avg_price = status["avg_fill_price"]
    # Prefer commission captured from the fill callback (execDetailsEvent) because
    # IB sends commission reports asynchronously — get_order_status may still
    # show 0 commission at this point even for a genuine fill.
    commission = _fill_commission if _fill_commission is not None else (status["commission"] or Decimal("0"))

    if qty_filled > 0 and avg_price is not None:
        if qty_filled >= qty:
            await _handle_fill(order, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
        else:
            await _handle_partial(order, trade_group, qty, qty_filled, avg_price, commission, cmd, con_id, ib_order_id, ctx)
    else:
        # No shares filled.  Distinguish between IB holding the order (Inactive)
        # and a genuine reprice timeout.
        _ib_final_status = status["status"]
        _why_held = status.get("why_held") or ""

        if _ib_final_status == "Inactive":
            _err = ctx.ib.get_order_error(ib_order_id)
            reason = _err or f"IB set order Inactive"
            if _why_held:
                reason += f" (whyHeld: {_why_held!r})"
            _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, cmd.symbol, side, "LIMIT",
                       qty, ib_order_id=_safe_int(ib_order_id),
                       trade_serial=trade_group.serial_number)
            await ctx.ib.cancel_order(ib_order_id)
            ctx.orders.update_status(order.id, OrderStatus.CANCELED)
            ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
            _write_txn(ctx, TransactionAction.CANCELLED, cmd.symbol, side, "LIMIT",
                       qty, ib_order_id=_safe_int(ib_order_id),
                       ib_error_message=reason,
                       trade_serial=trade_group.serial_number, is_terminal=True,
                       ib_responded_at=_now_utc())
            ctx.router.emit(
                f"\u2717 INACTIVE: {qty} {cmd.symbol} \u2014 {reason}\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
                event="ORDER_INACTIVE_DISPLAY",
            )
            logger.error(
                '{"event": "ORDER_INACTIVE", "order_id": "%s", "serial": %d, '
                '"symbol": "%s", "reason": "%s", "why_held": "%s"}',
                order.id, trade_group.serial_number, cmd.symbol, reason, _why_held,
            )
        else:
            # Normal reprice window expired with no fill.
            _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, cmd.symbol, side, "LIMIT",
                       qty, ib_order_id=_safe_int(ib_order_id),
                       trade_serial=trade_group.serial_number)
            await ctx.ib.cancel_order(ib_order_id)
            ctx.orders.update_status(order.id, OrderStatus.CANCELED)
            ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
            _write_txn(ctx, TransactionAction.CANCELLED, cmd.symbol, side, "LIMIT",
                       qty, ib_order_id=_safe_int(ib_order_id),
                       trade_serial=trade_group.serial_number, is_terminal=True,
                       ib_responded_at=_now_utc())
            ctx.router.emit(
                f"\u2717 EXPIRED: 0/{qty} filled | reprice window closed "
                f"({settings['reprice_duration_seconds']}s)\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                event="ORDER_EXPIRED_DISPLAY",
            )
            logger.info(
                '{"event": "ORDER_EXPIRED", "order_id": "%s", "serial": %d, '
                '"reason": "reprice_timeout_no_fill"}',
                order.id, trade_group.serial_number,
            )

    ctx.tracker.unregister(ib_order_id)


async def _execute_bid_ask_order(
    cmd, order: Order, trade_group: TradeGroup, con_id: int,
    side: str, qty: Decimal, ctx: AppContext,
) -> None:
    """Place a limit order fixed at the current bid or ask price — no reprice loop.

    'bid' strategy: place at the current bid.
      - For SELL (short): aggressive — sells immediately to waiting buyers.
      - For BUY:          passive  — waits for a seller to come down to you.

    'ask' strategy: place at the current ask.
      - For BUY:          aggressive — buys immediately from waiting sellers.
      - For SELL (short): passive  — waits for a buyer to come up to you.

    The order is GTC. If it does not fill within 30 seconds the REPL moves on
    and the daemon reconciler will catch the eventual fill.
    """
    snapshot = await ctx.ib.get_market_snapshot(con_id)
    bid, ask, last = snapshot["bid"], snapshot["ask"], snapshot["last"]

    if bid == 0 and ask == 0:
        if last == 0:
            raise ValueError(
                f"Cannot place order for {cmd.symbol}: no market data available "
                "(bid=0, ask=0, last=0). Check market data subscription."
            )
        logger.warning(
            '{"event": "NO_BID_ASK_USING_LAST", "symbol": "%s", "last": "%s", '
            '"reason": "bid/ask unavailable, market likely closed"}',
            cmd.symbol, last,
        )
        bid = ask = last

    price = ask if cmd.strategy == Strategy.ASK else bid

    ctx.router.emit(
        f"Order #{trade_group.serial_number} \u2014 {side} {qty} {cmd.symbol} @ {cmd.strategy}\n"
        f"[{_now_utc().strftime('%H:%M:%S')}] Placed @ ${price} "
        f"(bid: ${bid} ask: ${ask})",
        pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
        event="ORDER_PLACED_BID_ASK",
    )

    _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "LIMIT",
               qty, limit_price=price, trade_serial=trade_group.serial_number)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, price, outside_rth=True, tif=_session_tif()
    )

    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
               qty, limit_price=price, ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, ib_responded_at=_now_utc())

    ctx.orders.update_ib_order_id(order.id, ib_order_id)
    ctx.orders.update_status(order.id, OrderStatus.OPEN)
    ctx.orders.set_raw_response(order.id, json.dumps({"ib_order_id": ib_order_id, "price": str(price)}))

    track = ctx.tracker.register(order.id, ib_order_id, cmd.symbol)

    _fill_commission: Decimal | None = None

    async def on_fill(fill_ib_id: str, _qty: Decimal, _avg: Decimal, commission: Decimal):
        nonlocal _fill_commission
        if fill_ib_id == ib_order_id:
            _fill_commission = commission
            ctx.tracker.notify_filled(fill_ib_id)

    async def on_status(status_ib_id: str, status: str):
        if status_ib_id == ib_order_id and status in ("Cancelled", "Inactive"):
            ctx.tracker.notify_canceled(status_ib_id)

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)
    ctx.ib.register_status_callback(on_status, ib_order_id=ib_order_id)

    # Wait briefly to catch immediate fills (e.g. ask buy fills at once).
    # If not filled within bid_ask_wait_seconds, leave the GTC order live —
    # daemon reconciler will update the DB when IB eventually reports the fill.
    bid_ask_wait = float(ctx.settings.get("bid_ask_wait_seconds", 30))
    try:
        await asyncio.wait_for(track.fill_event.wait(), timeout=bid_ask_wait)
    except asyncio.TimeoutError:
        pass

    status = await ctx.ib.get_order_status(ib_order_id)
    ib_status = status["status"]
    qty_filled = status["qty_filled"]
    avg_price = status["avg_fill_price"]
    commission = _fill_commission if _fill_commission is not None else (status["commission"] or Decimal("0"))

    if qty_filled > 0 and avg_price is not None:
        if qty_filled >= qty:
            await _handle_fill(order, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
        else:
            await _handle_partial(order, trade_group, qty, qty_filled, avg_price, commission, cmd, con_id, ib_order_id, ctx)
    elif track.is_canceled:
        # IB cancelled or set the order Inactive (notify_canceled fires for both).
        # Check actual IB status for the right message and reason.
        _err = ctx.ib.get_order_error(ib_order_id) or ""
        _why = status.get("why_held") or ""
        if ib_status == "Inactive":
            # Inactive = IB is holding the order due to an error condition.
            # Always comes with an error code; whyHeld provides detail.
            # Source: https://interactivebrokers.github.io/tws-api/order_submission.html
            reason = _err or f"IB set order Inactive"
            if _why:
                reason += f" (whyHeld: {_why!r})"
            event_label = "INACTIVE"
        else:
            reason = _err or "IB rejected or cancelled the order"
            event_label = "REJECTED"
        ctx.orders.update_status(order.id, OrderStatus.CANCELED)
        ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
        ctx.router.emit(
            f"\u2717 {event_label}: {qty} {cmd.symbol} \u2014 {reason}\n"
            f"  Serial: #{trade_group.serial_number}",
            pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
            event=f"ORDER_{event_label}_DISPLAY",
        )
        logger.error(
            '{"event": "ORDER_%s", "order_id": "%s", "serial": %d, '
            '"symbol": "%s", "reason": "%s"}',
            event_label, order.id, trade_group.serial_number, cmd.symbol, reason,
        )
    elif ib_status == "PreSubmitted":
        _why = status.get("why_held") or ""
        _expected_reason = presubmitted_reason()
        if not is_ib_session_active():
            # Weekend closure or session break — expected, leave OPEN.
            ctx.router.emit(
                f"\u26a0 QUEUED: {qty} {cmd.symbol} @ ${price} ({cmd.strategy}) \u2014 "
                f"{_expected_reason}\n"
                f"  GTC order held by IB; daemon reconciler will catch the fill.\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                event="ORDER_QUEUED_DISPLAY",
            )
            logger.info(
                '{"event": "ORDER_QUEUED", "order_id": "%s", "serial": %d, '
                '"symbol": "%s", "price": "%s", "reason": "%s"}',
                order.id, trade_group.serial_number, cmd.symbol, price, _expected_reason,
            )
        else:
            # Active session — should be Submitted, not PreSubmitted. Cancel it.
            _detail = f" (IB whyHeld: {_why!r})" if _why else ""
            reason = (
                f"Order not working at exchange during {session_label()}"
                f"{_detail}. IB accepted but did not route."
            )
            await ctx.ib.cancel_order(ib_order_id)
            ctx.orders.update_status(order.id, OrderStatus.ABANDONED)
            ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
            ctx.router.emit(
                f"\u2717 NOT ACTIVE: {qty} {cmd.symbol} \u2014 {reason}\n"
                f"  Check IB Gateway logs and account permissions.\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
                event="ORDER_PRESUBMITTED_UNEXPECTED_DISPLAY",
            )
            logger.error(
                '{"event": "ORDER_NOT_ROUTED", "order_id": "%s", "serial": %d, '
                '"symbol": "%s", "strategy": "%s", "why_held": "%s"}',
                order.id, trade_group.serial_number, cmd.symbol, cmd.strategy, _why,
            )
    else:
        # Order live in IB as GTC — leave trade/order OPEN, daemon will reconcile the fill.
        ctx.router.emit(
            f"\u25cf LIVE: {qty} {cmd.symbol} @ ${price} ({cmd.strategy}) — "
            f"GTC order active in IB\n"
            f"  Serial: #{trade_group.serial_number}",
            pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
            event="ORDER_LIVE_GTC_DISPLAY",
        )
        logger.info(
            '{"event": "ORDER_LIVE_GTC", "order_id": "%s", "serial": %d, '
            '"symbol": "%s", "price": "%s", "strategy": "%s"}',
            order.id, trade_group.serial_number, cmd.symbol, price, cmd.strategy,
        )

    ctx.tracker.unregister(ib_order_id)


async def _execute_market_order(
    cmd, order: Order, trade_group: TradeGroup, con_id: int,
    side: str, qty: Decimal, ctx: AppContext,
) -> None:
    """Place a market order and wait for fill.

    During overnight session, the Blue Ocean ATS does not support market
    orders.  We auto-convert to an aggressive limit at the ask (BUY) or
    bid (SELL) so the order fills immediately at the best available price.
    """
    if is_overnight_session():
        # Overnight venue rejects market orders — convert to aggressive limit
        snapshot = await ctx.ib.get_market_snapshot(con_id)
        bid, ask = snapshot["bid"], snapshot["ask"]
        if bid == 0 and ask == 0:
            bid = ask = snapshot["last"]
        aggressive_price = ask if side == "BUY" else bid
        if aggressive_price == 0:
            raise ValueError(
                f"Cannot place market-equivalent order for {cmd.symbol}: "
                "no market data available (bid=0, ask=0, last=0)."
            )
        ctx.router.emit(
            f"Overnight session — converting market to limit @ ${aggressive_price}",
            pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
            event="MARKET_TO_LIMIT_OVERNIGHT",
        )
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "LIMIT",
                   qty, limit_price=aggressive_price,
                   trade_serial=trade_group.serial_number)
        ib_order_id = await ctx.ib.place_limit_order(
            con_id, cmd.symbol, side, qty, aggressive_price,
            outside_rth=True, tif=_session_tif(),
        )
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
                   qty, limit_price=aggressive_price,
                   ib_order_id=_safe_int(ib_order_id),
                   trade_serial=trade_group.serial_number, ib_responded_at=_now_utc())
    else:
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "MARKET",
                   qty, trade_serial=trade_group.serial_number)
        ib_order_id = await ctx.ib.place_market_order(con_id, cmd.symbol, side, qty, outside_rth=True)
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "MARKET",
                   qty, ib_order_id=_safe_int(ib_order_id),
                   trade_serial=trade_group.serial_number, ib_responded_at=_now_utc())

    ctx.orders.update_ib_order_id(order.id, ib_order_id)
    ctx.orders.update_status(order.id, OrderStatus.OPEN)

    track = ctx.tracker.register(order.id, ib_order_id, cmd.symbol)

    async def on_fill(fill_ib_id: str, qty_filled: Decimal, avg_price: Decimal, commission: Decimal):
        if fill_ib_id == ib_order_id:
            ctx.tracker.notify_filled(fill_ib_id)

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)

    # Market orders should fill quickly — wait up to 30 seconds
    try:
        await asyncio.wait_for(track.fill_event.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        pass

    status = await ctx.ib.get_order_status(ib_order_id)
    qty_filled = status["qty_filled"]
    avg_price = status["avg_fill_price"]
    commission = status["commission"] or Decimal("0")

    if qty_filled >= qty:
        await _handle_fill(order, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
    elif qty_filled > 0:
        await _handle_partial(order, trade_group, qty, qty_filled, avg_price, commission, cmd, con_id, ib_order_id, ctx)
    else:
        ctx.orders.update_status(order.id, OrderStatus.CANCELED)
        ctx.router.emit(
            f"\u2717 CANCELED: market order did not fill\n  Serial: #{trade_group.serial_number}",
            pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
            event="ORDER_CANCELED_DISPLAY",
        )

    ctx.tracker.unregister(ib_order_id)


async def _handle_fill(
    order: Order, trade_group: TradeGroup, qty_filled: Decimal,
    avg_price: Decimal, commission: Decimal, cmd, con_id: int, ctx: AppContext,
) -> None:
    """Record a complete fill and place profit taker if configured."""
    ctx.orders.update_fill(order.id, qty_filled, avg_price, commission)
    ctx.orders.update_status(order.id, OrderStatus.FILLED)
    ctx.trades.update_pnl(trade_group.id, Decimal("0"), commission)

    _write_txn(ctx, TransactionAction.FILLED, order.symbol, order.side,
               order.order_type, qty_filled,
               ib_order_id=_safe_int(order.ib_order_id),
               ib_status="Filled", ib_filled_qty=qty_filled,
               ib_avg_fill_price=avg_price,
               trade_serial=trade_group.serial_number, is_terminal=True,
               ib_responded_at=_now_utc())

    ctx.router.emit(
        f"\u2713 FILLED: {qty_filled} shares {order.symbol} @ ${avg_price} avg\n"
        f"  Commission: ${commission}\n"
        f"  Serial: #{trade_group.serial_number}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
        event="ORDER_FILLED_DISPLAY",
    )
    logger.info(
        '{"event": "ORDER_FILLED", "order_id": "%s", "serial": %d, "symbol": "%s", '
        '"qty_filled": "%s", "avg_price": "%s", "commission": "%s"}',
        order.id, trade_group.serial_number, order.symbol, qty_filled, avg_price, commission,
    )

    # Place profit taker if configured
    if cmd.take_profit_price or cmd.profit_amount:
        from ib_trader.repl.commands import BuyCommand as BC
        entry_side = "BUY" if isinstance(cmd, BC) else "SELL"
        await place_profit_taker(
            trade_id=trade_group.id,
            entry_order_id=order.id,
            entry_side=entry_side,
            avg_fill_price=avg_price,
            qty_filled=qty_filled,
            profit_amount=cmd.profit_amount,
            take_profit_price=cmd.take_profit_price,
            con_id=con_id,
            symbol=order.symbol,
            ctx=ctx,
        )


async def _handle_partial(
    order: Order, trade_group: TradeGroup, qty_requested: Decimal,
    qty_filled: Decimal, avg_price: Decimal, commission: Decimal,
    cmd, con_id: int, ib_order_id: str, ctx: AppContext,
) -> None:
    """Record a partial fill and cancel the remaining quantity in IB."""
    _write_txn(ctx, TransactionAction.PARTIAL_FILL, order.symbol, order.side,
               order.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               ib_filled_qty=qty_filled, ib_avg_fill_price=avg_price,
               trade_serial=trade_group.serial_number,
               ib_responded_at=_now_utc())
    _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, order.symbol, order.side,
               order.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number)
    await ctx.ib.cancel_order(ib_order_id)
    ctx.orders.update_fill(order.id, qty_filled, avg_price, commission)
    ctx.orders.update_status(order.id, OrderStatus.PARTIAL)
    ctx.trades.update_pnl(trade_group.id, Decimal("0"), commission)
    _write_txn(ctx, TransactionAction.CANCELLED, order.symbol, order.side,
               order.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, is_terminal=True,
               ib_responded_at=_now_utc())

    remainder = qty_requested - qty_filled
    ctx.router.emit(
        f"\u26a0 PARTIAL: {qty_filled}/{qty_requested} filled @ avg ${avg_price} | "
        f"{remainder} shares canceled (timeout)\n"
        f"  Commission: ${commission}\n"
        f"  Serial: #{trade_group.serial_number}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
        event="ORDER_PARTIAL_DISPLAY",
    )
    logger.info(
        '{"event": "ORDER_PARTIAL_FILL", "order_id": "%s", "serial": %d, '
        '"qty_filled": "%s", "qty_requested": "%s"}',
        order.id, trade_group.serial_number, qty_filled, qty_requested,
    )

    if cmd.take_profit_price or cmd.profit_amount:
        from ib_trader.repl.commands import BuyCommand as BC
        entry_side = "BUY" if isinstance(cmd, BC) else "SELL"
        await place_profit_taker(
            trade_id=trade_group.id,
            entry_order_id=order.id,
            entry_side=entry_side,
            avg_fill_price=avg_price,
            qty_filled=qty_filled,
            profit_amount=cmd.profit_amount,
            take_profit_price=cmd.take_profit_price,
            con_id=con_id,
            symbol=order.symbol,
            ctx=ctx,
        )


async def reprice_loop(
    order_id: str,
    ib_order_id: str,
    con_id: int,
    symbol: str,
    side: str,
    ctx: AppContext,
    total_steps: int,
    interval_seconds: float,
    initial_price: Decimal,
) -> None:
    """Reprice loop: amend the order toward the ask over total_steps iterations.

    Each step:
    - Fetches live bid/ask.
    - Calculates the next price step (rounded to 2dp).
    - Skips the amendment if the rounded price is unchanged from the last sent
      price — avoids redundant IB API calls when a tight spread produces
      several identical 2dp values across consecutive steps.
    - Amends the existing IB order in place when the price changes.
    - Writes a RepriceEvent to SQLite only for actual amendments.

    Exits early if the fill_event is set (order filled).

    Args:
        order_id: Internal UUID of the order.
        ib_order_id: IB-assigned order ID (single ID throughout all amendments).
        con_id: IB contract ID for market data snapshots.
        symbol: Ticker symbol.
        side: "BUY" or "SELL".
        ctx: Application context.
        total_steps: Total number of reprice steps.
        interval_seconds: Sleep interval between steps.
        initial_price: The price the order was originally placed at.  Used as
            the baseline for deduplication so the first step is also skipped
            when rounding produces the same value.
    """
    ctx.orders.update_status(order_id, OrderStatus.AMENDING)

    last_sent_price: Decimal = initial_price

    for step in range(1, total_steps + 1):
        track = ctx.tracker.get(ib_order_id)
        if track and (track.is_filled or track.is_canceled):
            break

        await asyncio.sleep(interval_seconds)

        track = ctx.tracker.get(ib_order_id)
        if track and (track.is_filled or track.is_canceled):
            break

        snapshot = await ctx.ib.get_market_snapshot(con_id)
        bid, ask, last = snapshot["bid"], snapshot["ask"], snapshot["last"]

        if bid == 0 and ask == 0:
            ref = last if last > 0 else Decimal("0")
            if ref == 0:
                logger.warning(
                    '{"event": "REPRICE_SKIPPED_NO_DATA", "symbol": "%s", '
                    '"ib_order_id": "%s", "step": %d}',
                    symbol, ib_order_id, step,
                )
                continue
            bid = ask = ref

        new_price = calc_step_price(bid, ask, step, total_steps, side)

        if new_price == last_sent_price:
            logger.debug(
                '{"event": "REPRICE_SKIPPED_SAME_PRICE", "order_id": "%s", '
                '"step": %d, "price": "%s"}',
                order_id, step, new_price,
            )
            continue

        await ctx.ib.amend_order(ib_order_id, new_price)
        last_sent_price = new_price

        # Write amendment to SQLite
        now = datetime.now(timezone.utc)
        evt = RepriceEvent(
            order_id=order_id,
            step_number=step,
            bid=bid,
            ask=ask,
            new_price=new_price,
            amendment_confirmed=False,
            timestamp=now,
        )
        evt = ctx.reprice_events.create(evt)
        ctx.orders.update_amended(order_id, new_price)

        # Get current fill status for display
        status = await ctx.ib.get_order_status(ib_order_id)
        qty_filled = status["qty_filled"]

        ctx.router.emit(
            f"[{now.strftime('%H:%M:%S')}] Amended \u2192 ${new_price} | "
            f"step {step}/{total_steps} "
            f"(still open: {qty_filled}/? filled)",
            pane=OutputPane.LOG, severity=OutputSeverity.INFO,
            event="REPRICE_STEP_DISPLAY",
        )
        logger.info(
            '{"event": "REPRICE_STEP", "order_id": "%s", "step": %d, "total": %d, '
            '"bid": "%s", "ask": "%s", "new_price": "%s"}',
            order_id, step, total_steps, bid, ask, new_price,
        )

    logger.info('{"event": "REPRICE_TIMEOUT", "order_id": "%s"}', order_id)


async def place_profit_taker(
    trade_id: str,
    entry_order_id: str,
    entry_side: str,
    avg_fill_price: Decimal,
    qty_filled: Decimal,
    profit_amount: Decimal | None,
    take_profit_price: Decimal | None,
    con_id: int,
    symbol: str,
    ctx: AppContext,
) -> None:
    """Place a GTC profit taker order after an entry fill.

    Profit taker side is always the inverse of entry side:
    - BUY entry → SELL profit taker
    - SELL entry → BUY profit taker (cover lower)

    Price calculation:
    - BUY entry: profit_price = avg_fill_price + (profit_amount / qty_filled)
    - SELL entry: profit_price = avg_fill_price - (profit_amount / qty_filled)
    - If take_profit_price is given, use it directly regardless of side.

    Args:
        trade_id: Trade group UUID.
        entry_order_id: Entry order UUID.
        entry_side: "BUY" or "SELL".
        avg_fill_price: Average fill price of the entry leg.
        qty_filled: Quantity filled on the entry leg.
        profit_amount: Total dollar profit target (or None).
        take_profit_price: Explicit profit taker price (or None).
        con_id: IB contract ID.
        symbol: Ticker symbol.
        ctx: Application context.
    """
    pt_side = "SELL" if entry_side == "BUY" else "BUY"

    if take_profit_price is not None:
        pt_price = take_profit_price
    elif profit_amount is not None:
        if entry_side == "BUY":
            pt_price = calc_profit_taker_price(avg_fill_price, qty_filled, profit_amount)
        else:
            pt_price = calc_profit_taker_price_short(avg_fill_price, qty_filled, profit_amount)
    else:
        return

    _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, symbol, pt_side, "LIMIT",
               qty_filled, limit_price=pt_price)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, symbol, pt_side, qty_filled, pt_price,
        outside_rth=True, tif=_session_tif(),
    )

    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, symbol, pt_side, "LIMIT",
               qty_filled, limit_price=pt_price, ib_order_id=_safe_int(ib_order_id),
               ib_responded_at=_now_utc())

    pt_order = Order(
        trade_id=trade_id,
        ib_order_id=ib_order_id,
        leg_type=LegType.PROFIT_TAKER,
        symbol=symbol,
        side=pt_side,
        security_type=SecurityType.STK,
        qty_requested=qty_filled,
        qty_filled=Decimal("0"),
        order_type="MID",
        price_placed=pt_price,
        status=OrderStatus.OPEN,
        placed_at=datetime.now(timezone.utc),
    )
    ctx.orders.create(pt_order)

    ctx.router.emit(
        f"  Profit taker placed @ ${pt_price}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
        event="PROFIT_TAKER_PLACED_DISPLAY",
    )
    logger.info(
        '{"event": "PROFIT_TAKER_PLACED", "trade_id": "%s", "symbol": "%s", '
        '"side": "%s", "price": "%s", "ib_order_id": "%s"}',
        trade_id, symbol, pt_side, pt_price, ib_order_id,
    )


async def _handle_close_fill(
    close_order: Order, trade_group: TradeGroup, entry_order: Order,
    qty_filled: Decimal, avg_price: Decimal, commission: Decimal,
    ctx: AppContext,
) -> None:
    """Record a complete close fill, compute P&L, and close the trade group."""
    ctx.orders.update_fill(close_order.id, qty_filled, avg_price, commission)
    ctx.orders.update_status(close_order.id, OrderStatus.FILLED)

    _write_txn(ctx, TransactionAction.FILLED, close_order.symbol, close_order.side,
               close_order.order_type, qty_filled,
               ib_order_id=_safe_int(close_order.ib_order_id),
               ib_status="Filled", ib_filled_qty=qty_filled,
               ib_avg_fill_price=avg_price,
               trade_serial=trade_group.serial_number, is_terminal=True,
               ib_responded_at=_now_utc())

    # Compute realized P&L: (exit - entry) * qty * direction - total commission
    entry_price = entry_order.avg_fill_price or Decimal("0")
    entry_commission = entry_order.commission or Decimal("0")
    direction = Decimal("1") if entry_order.side == "BUY" else Decimal("-1")
    realized_pnl = (avg_price - entry_price) * qty_filled * direction
    total_commission = commission + entry_commission
    realized_pnl -= total_commission

    # Check if the full position is now closed
    all_legs = ctx.orders.get_for_trade(trade_group.id)
    remaining = entry_order.qty_filled
    for leg in all_legs:
        if leg.leg_type in (LegType.CLOSE, LegType.PROFIT_TAKER) and leg.status == OrderStatus.FILLED:
            remaining -= leg.qty_filled

    ctx.trades.update_pnl(trade_group.id, realized_pnl, total_commission)
    if remaining <= 0:
        ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)

    pnl_str = f"+${realized_pnl}" if realized_pnl >= 0 else f"-${abs(realized_pnl)}"
    closed_label = "CLOSED" if remaining <= 0 else "PARTIAL CLOSE"
    ctx.router.emit(
        f"\u2713 {closed_label}: {qty_filled} shares {close_order.symbol} @ ${avg_price}\n"
        f"  P&L: {pnl_str} (commission: ${total_commission})\n"
        f"  Serial: #{trade_group.serial_number}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
        event="CLOSE_ORDER_FILLED",
    )
    logger.info(
        '{"event": "CLOSE_ORDER_FILLED", "order_id": "%s", "serial": %d, '
        '"qty_filled": "%s", "avg_price": "%s", "realized_pnl": "%s"}',
        close_order.id, trade_group.serial_number, qty_filled, avg_price, realized_pnl,
    )


async def _handle_close_partial(
    close_order: Order, trade_group: TradeGroup, entry_order: Order,
    qty_requested: Decimal, qty_filled: Decimal, avg_price: Decimal,
    commission: Decimal, ib_order_id: str, ctx: AppContext,
) -> None:
    """Record a partial close fill and cancel the remaining quantity."""
    await ctx.ib.cancel_order(ib_order_id)
    ctx.orders.update_fill(close_order.id, qty_filled, avg_price, commission)
    ctx.orders.update_status(close_order.id, OrderStatus.PARTIAL)

    entry_price = entry_order.avg_fill_price or Decimal("0")
    entry_commission = entry_order.commission or Decimal("0")
    direction = Decimal("1") if entry_order.side == "BUY" else Decimal("-1")
    realized_pnl = (avg_price - entry_price) * qty_filled * direction
    # Prorate entry commission by fill ratio
    prorated_entry_comm = entry_commission * qty_filled / entry_order.qty_filled if entry_order.qty_filled else Decimal("0")
    total_commission = commission + prorated_entry_comm
    realized_pnl -= total_commission

    ctx.trades.update_pnl(trade_group.id, realized_pnl, total_commission)

    remainder = qty_requested - qty_filled
    pnl_str = f"+${realized_pnl}" if realized_pnl >= 0 else f"-${abs(realized_pnl)}"
    ctx.router.emit(
        f"\u26a0 CLOSE PARTIAL: {qty_filled}/{qty_requested} filled @ ${avg_price}\n"
        f"  {remainder} shares still open. P&L on closed portion: {pnl_str}\n"
        f"  Serial: #{trade_group.serial_number}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
        event="CLOSE_ORDER_PARTIAL",
    )
    logger.info(
        '{"event": "CLOSE_ORDER_PARTIAL", "order_id": "%s", "serial": %d, '
        '"qty_filled": "%s", "qty_requested": "%s", "realized_pnl": "%s"}',
        close_order.id, trade_group.serial_number, qty_filled, qty_requested, realized_pnl,
    )


async def execute_close(cmd: "CloseCommand", ctx: AppContext) -> None:
    """Close an open position by serial number.

    - Looks up the trade group by serial number.
    - Cancels all linked open IB orders (profit taker, stop loss).
    - Places a closing order (inverse side of entry).
    - Logs as CLOSED_MANUAL.

    Args:
        cmd: Parsed CloseCommand.
        ctx: Application context.
    """
    trade_group = ctx.trades.get_by_serial(cmd.serial)
    if not trade_group:
        ctx.router.emit(
            f"\u2717 Error: no trade found with serial #{cmd.serial}",
            pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
        )
        raise TradeNotFoundError(f"No trade with serial #{cmd.serial}")

    # Find entry leg
    entry_orders = ctx.orders.get_in_states([
        OrderStatus.FILLED, OrderStatus.PARTIAL
    ])
    entry_order = None
    for o in entry_orders:
        if o.trade_id == trade_group.id and o.leg_type == LegType.ENTRY:
            entry_order = o
            break

    if not entry_order:
        ctx.router.emit(
            f"\u2717 Error: order #{cmd.serial} has no filled quantity to close",
            pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
        )
        return

    qty_to_close = entry_order.qty_filled

    # Subtract already-filled close legs and profit takers to avoid over-closing
    all_legs = ctx.orders.get_for_trade(trade_group.id)
    for leg in all_legs:
        if leg.leg_type in (LegType.CLOSE, LegType.PROFIT_TAKER) and leg.status == OrderStatus.FILLED:
            qty_to_close -= leg.qty_filled

    if qty_to_close <= 0:
        ctx.router.emit(
            f"\u2717 Warning: trade #{cmd.serial} is already fully closed",
            pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
        )
        return

    # Cancel all linked open orders (profit taker, stop loss)
    open_legs = ctx.orders.get_open_for_trade(trade_group.id)
    for leg in open_legs:
        if leg.leg_type in (LegType.PROFIT_TAKER, LegType.STOP_LOSS) and leg.ib_order_id:
            _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, leg.symbol, leg.side,
                       leg.order_type, leg.qty_requested,
                       ib_order_id=_safe_int(leg.ib_order_id),
                       trade_serial=trade_group.serial_number)
            await ctx.ib.cancel_order(leg.ib_order_id)
            ctx.orders.update_status(leg.id, OrderStatus.CANCELED)
            _write_txn(ctx, TransactionAction.CANCELLED, leg.symbol, leg.side,
                       leg.order_type, leg.qty_requested,
                       ib_order_id=_safe_int(leg.ib_order_id),
                       trade_serial=trade_group.serial_number, is_terminal=True,
                       ib_responded_at=_now_utc())
            logger.info(
                '{"event": "PROFIT_TAKER_CANCELED", "order_id": "%s", "reason": "close"}',
                leg.id,
            )

    # Determine close side (inverse of entry)
    close_side = "SELL" if entry_order.side == "BUY" else "BUY"

    # Get contract
    contract_info = await _get_contract(entry_order.symbol, ctx)
    con_id = contract_info["con_id"]

    # Create close order record
    close_order = Order(
        trade_id=trade_group.id,
        leg_type=LegType.CLOSE,
        symbol=entry_order.symbol,
        side=close_side,
        security_type=SecurityType.STK,
        qty_requested=qty_to_close,
        qty_filled=Decimal("0"),
        order_type=cmd.strategy.upper(),
        profit_taker_amount=cmd.profit_amount,
        profit_taker_price=cmd.take_profit_price,
        status=OrderStatus.PENDING,
        placed_at=datetime.now(timezone.utc),
    )
    close_order = ctx.orders.create(close_order)

    logger.info(
        '{"event": "ORDER_CLOSED_MANUAL", "trade_id": "%s", "serial": %d, '
        '"symbol": "%s", "qty": "%s"}',
        trade_group.id, cmd.serial, entry_order.symbol, qty_to_close,
    )

    # ── Place IB order (strategy-dependent) ────────────────────────────────
    initial_price = Decimal("0")
    _close_order_type = cmd.strategy.upper() if cmd.strategy != Strategy.MARKET else "MARKET"
    if cmd.strategy == Strategy.LIMIT:
        initial_price = cmd.limit_price
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_order.symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   trade_serial=cmd.serial)
        ib_order_id = await ctx.ib.place_limit_order(
            con_id, entry_order.symbol, close_side, qty_to_close, initial_price,
            outside_rth=True, tif=_session_tif(),
        )
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_order.symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   ib_order_id=_safe_int(ib_order_id), trade_serial=cmd.serial,
                   ib_responded_at=_now_utc())
        ctx.orders.update_ib_order_id(close_order.id, ib_order_id)
        ctx.orders.update_amended(close_order.id, initial_price)
        ctx.orders.update_status(close_order.id, OrderStatus.OPEN)
        ctx.router.emit(
            f"Close #{cmd.serial} limit @ ${initial_price} placed.",
            pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
            event="CLOSE_ORDER_PLACED",
        )
    elif cmd.strategy == Strategy.MID:
        snapshot = await ctx.ib.get_market_snapshot(con_id)
        bid, ask, last = snapshot["bid"], snapshot["ask"], snapshot["last"]
        if bid == 0 and ask == 0:
            if last == 0:
                raise ValueError(
                    f"Cannot close {entry_order.symbol}: no market data available "
                    "(bid=0, ask=0, last=0). Check market data subscription."
                )
            bid = ask = last
        initial_price = calc_mid(bid, ask)
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_order.symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   trade_serial=cmd.serial)
        ib_order_id = await ctx.ib.place_limit_order(
            con_id, entry_order.symbol, close_side, qty_to_close, initial_price,
            outside_rth=True, tif=_session_tif(),
        )
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_order.symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   ib_order_id=_safe_int(ib_order_id), trade_serial=cmd.serial,
                   ib_responded_at=_now_utc())
        ctx.orders.update_ib_order_id(close_order.id, ib_order_id)
        ctx.orders.update_status(close_order.id, OrderStatus.REPRICING)
        ctx.router.emit(
            f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Close #{cmd.serial} placed "
            f"@ ${initial_price} (bid: ${bid} ask: ${ask})",
            pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
            event="CLOSE_ORDER_PLACED",
        )
    elif cmd.strategy in (Strategy.BID, Strategy.ASK):
        snapshot = await ctx.ib.get_market_snapshot(con_id)
        bid, ask, last = snapshot["bid"], snapshot["ask"], snapshot["last"]
        if bid == 0 and ask == 0:
            if last == 0:
                raise ValueError(
                    f"Cannot close {entry_order.symbol}: no market data available "
                    "(bid=0, ask=0, last=0). Check market data subscription."
                )
            bid = ask = last
        initial_price = ask if cmd.strategy == Strategy.ASK else bid
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_order.symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   trade_serial=cmd.serial)
        ib_order_id = await ctx.ib.place_limit_order(
            con_id, entry_order.symbol, close_side, qty_to_close, initial_price,
            outside_rth=True, tif=_session_tif(),
        )
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_order.symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   ib_order_id=_safe_int(ib_order_id), trade_serial=cmd.serial,
                   ib_responded_at=_now_utc())
        ctx.orders.update_ib_order_id(close_order.id, ib_order_id)
        ctx.orders.update_status(close_order.id, OrderStatus.OPEN)
        ctx.router.emit(
            f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Close #{cmd.serial} placed "
            f"@ ${initial_price} ({cmd.strategy} — bid: ${bid} ask: ${ask})",
            pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
            event="CLOSE_ORDER_PLACED",
        )
    else:
        # Market order — immediate execution expected.
        # During overnight session, Blue Ocean ATS rejects market orders.
        # Convert to aggressive limit at the bid (SELL) or ask (BUY).
        if is_overnight_session():
            snapshot = await ctx.ib.get_market_snapshot(con_id)
            bid, ask = snapshot["bid"], snapshot["ask"]
            if bid == 0 and ask == 0:
                bid = ask = snapshot["last"]
            initial_price = bid if close_side == "SELL" else ask
            if initial_price == 0:
                raise ValueError(
                    f"Cannot close {entry_order.symbol}: no market data available "
                    "(bid=0, ask=0, last=0). Check market data subscription."
                )
            ctx.router.emit(
                f"Overnight session — converting market to limit @ ${initial_price}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                event="MARKET_TO_LIMIT_OVERNIGHT",
            )
            _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_order.symbol, close_side,
                       "LIMIT", qty_to_close, limit_price=initial_price,
                       trade_serial=cmd.serial)
            ib_order_id = await ctx.ib.place_limit_order(
                con_id, entry_order.symbol, close_side, qty_to_close, initial_price,
                outside_rth=True, tif=_session_tif(),
            )
            _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_order.symbol, close_side,
                       "LIMIT", qty_to_close, limit_price=initial_price,
                       ib_order_id=_safe_int(ib_order_id), trade_serial=cmd.serial,
                       ib_responded_at=_now_utc())
        else:
            _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_order.symbol, close_side,
                       "MARKET", qty_to_close, trade_serial=cmd.serial)
            ib_order_id = await ctx.ib.place_market_order(
                con_id, entry_order.symbol, close_side, qty_to_close, outside_rth=True
            )
            _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_order.symbol, close_side,
                       "MARKET", qty_to_close, ib_order_id=_safe_int(ib_order_id),
                       trade_serial=cmd.serial, ib_responded_at=_now_utc())
        ctx.orders.update_ib_order_id(close_order.id, ib_order_id)
        ctx.orders.update_status(close_order.id, OrderStatus.OPEN)
        ctx.router.emit(
            f"Close #{cmd.serial} market order placed.",
            pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
            event="CLOSE_ORDER_PLACED",
        )

    ctx.orders.set_raw_response(
        close_order.id,
        json.dumps({"ib_order_id": ib_order_id, "initial_price": str(initial_price)}),
    )

    # ── Register tracker + callbacks ─────────────────────────────────────
    track = ctx.tracker.register(close_order.id, ib_order_id, entry_order.symbol)
    _fill_commission: Decimal | None = None

    async def on_fill(fill_ib_id: str, _qty: Decimal, _avg: Decimal, commission: Decimal):
        nonlocal _fill_commission
        if fill_ib_id == ib_order_id:
            _fill_commission = commission
            ctx.tracker.notify_filled(fill_ib_id)

    async def on_status(status_ib_id: str, status: str):
        if status_ib_id == ib_order_id and status in ("Cancelled", "Inactive"):
            ctx.tracker.notify_canceled(status_ib_id)

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)
    ctx.ib.register_status_callback(on_status, ib_order_id=ib_order_id)

    # ── Poll for IB acknowledgment (limit orders only) ───────────────────
    if cmd.strategy != Strategy.MARKET:
        _SUBMIT_POLL_INTERVAL = 0.5
        _SUBMIT_POLL_STEPS = 20  # 10 s max
        _pending = {"", "PendingSubmit"}
        for _ in range(_SUBMIT_POLL_STEPS):
            _st = await ctx.ib.get_order_status(ib_order_id)
            _ib_status = _st["status"]
            _ib_err = ctx.ib.get_order_error(ib_order_id)
            if _ib_err and _ib_status in _pending:
                break
            if _ib_status not in _pending:
                break
            await asyncio.sleep(_SUBMIT_POLL_INTERVAL)
        else:
            _st = await ctx.ib.get_order_status(ib_order_id)
            _ib_status = _st["status"]
            _ib_err = ctx.ib.get_order_error(ib_order_id)

        if _ib_status in _pending or _ib_status in ("Cancelled", "Inactive"):
            await ctx.ib.cancel_order(ib_order_id)
            ctx.orders.update_status(close_order.id, OrderStatus.CANCELED)
            ctx.tracker.unregister(ib_order_id)
            reason = _ib_err or f"Close order not acknowledged (status: {_ib_status!r})"
            ctx.router.emit(
                f"\u2717 CLOSE FAILED: #{cmd.serial} — {reason}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
                event="CLOSE_ORDER_FAILED",
            )
            logger.error(
                '{"event": "CLOSE_ORDER_FAILED", "order_id": "%s", "serial": %d, '
                '"ib_order_id": "%s", "reason": "%s"}',
                close_order.id, cmd.serial, ib_order_id, reason,
            )
            return

    # ── Limit close: fire-and-forget (like buy/sell limit) ──────────────
    if cmd.strategy == Strategy.LIMIT:
        # Check for immediate fill (aggressive limit that crossed the spread)
        status = await ctx.ib.get_order_status(ib_order_id)
        qty_filled = status["qty_filled"]
        avg_price = status["avg_fill_price"]
        commission = (
            _fill_commission if _fill_commission is not None
            else (status["commission"] or Decimal("0"))
        )
        if qty_filled > 0 and avg_price is not None and qty_filled >= qty_to_close:
            await _handle_close_fill(
                close_order, trade_group, entry_order, qty_filled,
                avg_price, commission, ctx,
            )
            ctx.tracker.unregister(ib_order_id)
            return

        # Order is live — return immediately.
        ctx.router.emit(
            f"● CLOSE LIMIT LIVE: #{cmd.serial} {close_side} {qty_to_close} "
            f"{entry_order.symbol} @ ${initial_price}\n"
            f"  GTC order active in IB  |  IB ID: {ib_order_id}\n"
            f"  Order will persist until filled or manually cancelled.",
            pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
            event="CLOSE_LIMIT_LIVE_DISPLAY",
        )
        logger.info(
            '{"event": "CLOSE_LIMIT_LIVE", "order_id": "%s", "serial": %d, '
            '"symbol": "%s", "price": "%s", "ib_order_id": "%s"}',
            close_order.id, cmd.serial, entry_order.symbol, initial_price, ib_order_id,
        )
        # Don't unregister tracker — callbacks stay active for the app session.
        return

    # ── Start reprice loop (mid strategy only) ───────────────────────────
    settings = ctx.settings
    reprice_task = None
    if cmd.strategy == Strategy.MID:
        total_steps = int(
            float(settings["reprice_duration_seconds"])
            / float(settings["reprice_interval_seconds"])
        )
        reprice_task = asyncio.create_task(
            reprice_loop(
                order_id=close_order.id,
                ib_order_id=ib_order_id,
                con_id=con_id,
                symbol=entry_order.symbol,
                side=close_side,
                ctx=ctx,
                total_steps=total_steps,
                interval_seconds=float(settings["reprice_interval_seconds"]),
                initial_price=initial_price,
            )
        )
        wait_timeout = float(settings["reprice_duration_seconds"]) + 2
    elif cmd.strategy in (Strategy.BID, Strategy.ASK):
        wait_timeout = float(settings.get("bid_ask_wait_seconds", 30))
    else:
        wait_timeout = 30  # market order timeout

    # ── Wait for fill or timeout ─────────────────────────────────────────
    try:
        await asyncio.wait_for(track.fill_event.wait(), timeout=wait_timeout)
    except asyncio.TimeoutError:
        pass

    if reprice_task:
        reprice_task.cancel()
        try:
            await reprice_task
        except asyncio.CancelledError:
            pass

    # ── Determine outcome ────────────────────────────────────────────────
    status = await ctx.ib.get_order_status(ib_order_id)
    qty_filled = status["qty_filled"]
    avg_price = status["avg_fill_price"]
    commission = (
        _fill_commission if _fill_commission is not None
        else (status["commission"] or Decimal("0"))
    )

    # Fallback: if IB says the order is Filled but get_order_status still
    # reports 0 fills (race condition in ib_async's internal state), trust
    # the fill callback or the IB status string rather than the qty.
    if qty_filled == 0 and status["status"] == "Filled" and track.is_filled:
        logger.warning(
            '{"event": "CLOSE_FILL_RACE_CONDITION", "ib_order_id": "%s", '
            '"status": "Filled", "qty_filled": 0, "note": "retrying get_order_status"}',
            ib_order_id,
        )
        await asyncio.sleep(0.5)
        status = await ctx.ib.get_order_status(ib_order_id)
        qty_filled = status["qty_filled"]
        avg_price = status["avg_fill_price"]
        commission = (
            _fill_commission if _fill_commission is not None
            else (status["commission"] or Decimal("0"))
        )

    if qty_filled > 0 and avg_price is not None:
        if qty_filled >= qty_to_close:
            await _handle_close_fill(
                close_order, trade_group, entry_order, qty_filled,
                avg_price, commission, ctx,
            )
        else:
            await _handle_close_partial(
                close_order, trade_group, entry_order, qty_to_close,
                qty_filled, avg_price, commission, ib_order_id, ctx,
            )
    else:
        # No fill — cancel and report
        await ctx.ib.cancel_order(ib_order_id)
        ctx.orders.update_status(close_order.id, OrderStatus.CANCELED)
        ctx.router.emit(
            f"\u2717 CLOSE EXPIRED: #{cmd.serial} — 0/{qty_to_close} filled\n"
            f"  Close order timed out. Position remains open.",
            pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
            event="CLOSE_ORDER_EXPIRED",
        )
        logger.info(
            '{"event": "CLOSE_ORDER_EXPIRED", "order_id": "%s", "serial": %d}',
            close_order.id, cmd.serial,
        )

    ctx.tracker.unregister(ib_order_id)
