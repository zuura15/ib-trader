"""Core order execution logic.

execute_order: places and manages an entry order (buy or sell).
reprice_loop: manages the reprice loop for mid-price orders.
place_profit_taker: places a GTC profit taker after fill.

All engine functions receive AppContext and call IB exclusively through ctx.ib.
No engine function imports or references ib_insync directly.
"""
import asyncio
import dataclasses
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.config.context import AppContext
from ib_trader.repl.output_router import OutputPane, OutputSeverity
from ib_trader.data.models import (
    LegType, RepriceEvent, TradeGroup, TradeStatus,
    TransactionAction, TransactionEvent,
)
from ib_trader.engine.exceptions import IBOrderRejectedError, SafetyLimitError, TradeNotFoundError
from ib_trader.engine.market_hours import (
    is_ib_session_active, is_overnight_session, presubmitted_reason, session_label,
)


@dataclasses.dataclass
class _OrderContext:
    """Ephemeral order context carried through execution flow. Not persisted to DB."""
    trade_id: str
    trade_serial: int
    symbol: str
    side: str
    order_type: str
    qty_requested: Decimal
    leg_type: LegType
    correlation_id: str
    security_type: str = "STK"
    expiry: str | None = None
    strike: Decimal | None = None
    right: str | None = None
    ib_order_id: str | None = None
    order_ref: str | None = None  # IB orderRef tag (IBT:{bot_ref}:{symbol}:{side}:{serial})


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


def _now_display() -> str:
    """Local time formatted for user-visible output (not logs/DB — those stay UTC)."""
    return datetime.now().strftime('%H:%M:%S')


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
    trade_id: str | None = None,
    leg_type: LegType | None = None,
    commission: Decimal | None = None,
    price_placed: Decimal | None = None,
    correlation_id: str | None = None,
    security_type: str | None = None,
    expiry: str | None = None,
    strike: Decimal | None = None,
    right: str | None = None,
    raw_response: str | None = None,
) -> None:
    """Write a single TransactionEvent row to the audit log.

    """
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
        trade_id=trade_id,
        leg_type=leg_type,
        commission=commission,
        price_placed=price_placed,
        correlation_id=correlation_id,
        security_type=security_type,
        expiry=expiry,
        strike=strike,
        right=right,
        raw_response=raw_response,
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
    3. Create TradeGroup in DB + ephemeral _OrderContext.
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
    side = "BUY" if isinstance(cmd, BuyCommand) else "SELL"

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

    # 3. Create TradeGroup in DB + ephemeral _OrderContext
    serial = ctx.trades.next_serial_number()
    direction = "LONG" if side == "BUY" else "SHORT"
    correlation_id = str(uuid.uuid4())

    # Expose the serial to the renderer for structured API responses
    ctx.router.update_order_row(serial, {"symbol": cmd.symbol, "side": side})

    # Build trade_config JSON with profit taker / stop loss parameters
    _trade_cfg: dict = {}
    if cmd.profit_amount is not None:
        _trade_cfg["profit_taker_amount"] = str(cmd.profit_amount)
    if cmd.take_profit_price is not None:
        _trade_cfg["profit_taker_price"] = str(cmd.take_profit_price)
    if cmd.stop_loss is not None:
        _trade_cfg["stop_loss_requested"] = str(cmd.stop_loss)

    trade_group = TradeGroup(
        serial_number=serial,
        symbol=cmd.symbol,
        direction=direction,
        status=TradeStatus.OPEN,
        opened_at=_now_utc(),
        trade_config=json.dumps(_trade_cfg) if _trade_cfg else None,
    )
    trade_group = ctx.trades.create(trade_group)

    # Encode orderRef AFTER serial allocation (bot_ref comes from the command)
    order_ref = None
    if hasattr(cmd, 'bot_ref') and cmd.bot_ref:
        from ib_trader.engine.order_ref import encode as encode_order_ref
        side_code = "B" if side == "BUY" else "S"
        order_ref = encode_order_ref(cmd.bot_ref, cmd.symbol, side_code, serial)

    order_ctx = _OrderContext(
        trade_id=trade_group.id,
        trade_serial=serial,
        symbol=cmd.symbol,
        side=side,
        order_type=cmd.strategy.upper(),
        qty_requested=qty,
        leg_type=LegType.ENTRY,
        correlation_id=correlation_id,
        security_type="STK",
        order_ref=order_ref,
    )

    if cmd.stop_loss:
        logger.info(
            '{"event": "STOP_LOSS_STUB_RECEIVED", "correlation_id": "%s", "value": "%s"}',
            order_ctx.correlation_id, cmd.stop_loss,
        )

    logger.info(
        '{"event": "ORDER_CREATED", "trade_id": "%s", "serial": %d, "symbol": "%s", '
        '"side": "%s", "qty": "%s", "strategy": "%s"}',
        trade_group.id, serial, cmd.symbol, side, qty, cmd.strategy,
    )

    try:
        if cmd.strategy == Strategy.LIMIT:
            await _execute_limit_order(cmd, order_ctx, trade_group, con_id, side, qty, ctx)
        elif cmd.strategy == Strategy.MID:
            await _execute_mid_order(cmd, order_ctx, trade_group, con_id, side, qty, ctx)
        elif cmd.strategy in (Strategy.BID, Strategy.ASK):
            await _execute_bid_ask_order(cmd, order_ctx, trade_group, con_id, side, qty, ctx)
        else:
            await _execute_market_order(cmd, order_ctx, trade_group, con_id, side, qty, ctx)
    except Exception as e:
        logger.error(
            '{"event": "ORDER_ERROR", "correlation_id": "%s", "error": "%s"}',
            order_ctx.correlation_id, str(e), exc_info=True,
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
    cmd, order_ctx: _OrderContext, trade_group: TradeGroup, con_id: int,
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
               qty, limit_price=price, trade_serial=trade_group.serial_number,
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, price, outside_rth=True, tif=_session_tif(),
        order_ref=order_ctx.order_ref,
    )

    order_ctx.ib_order_id = str(ib_order_id)

    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
               qty, limit_price=price, ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type,
               price_placed=price,
               raw_response=json.dumps({
                   "ib_order_id": ib_order_id, "price": str(price), "strategy": "limit",
                   "order_ref": order_ctx.order_ref,
               }))

    # Register fill/status callbacks so SQLite gets updated on fill
    track = ctx.tracker.register(order_ctx.correlation_id, ib_order_id, cmd.symbol)

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
                   ib_responded_at=_now_utc(),
                   trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                   correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
        logger.error(
            '{"event": "LIMIT_ORDER_REJECTED", "correlation_id": "%s", '
            '"serial": %d, "ib_order_id": "%s", "reason": "%s"}',
            order_ctx.correlation_id, trade_group.serial_number, ib_order_id, reason,
        )
        raise IBOrderRejectedError(reason)

    # Check for immediate fill (aggressive limit that crossed the spread)
    status = await ctx.ib.get_order_status(ib_order_id)
    qty_filled = status["qty_filled"]
    avg_price = status["avg_fill_price"]
    commission = status["commission"] or Decimal("0")

    if qty_filled > 0 and avg_price is not None and qty_filled >= qty:
        # Fully filled immediately — handle like any other fill
        await _handle_fill(order_ctx, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
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
        '{"event": "LIMIT_ORDER_LIVE", "correlation_id": "%s", "serial": %d, '
        '"symbol": "%s", "price": "%s", "ib_order_id": "%s"}',
        order_ctx.correlation_id, trade_group.serial_number, cmd.symbol, price, ib_order_id,
    )

    # Don't unregister tracker — callbacks stay active for the app session.
    # Daemon reconciliation handles fills that occur after app restart.


async def _execute_mid_order(
    cmd, order_ctx: _OrderContext, trade_group: TradeGroup, con_id: int,
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
        f"[{_now_display()}] Placed @ ${mid} "
        f"(bid: ${bid} ask: ${ask})",
        pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
        event="ORDER_PLACED_MID",
    )

    # PLACE_ATTEMPT before IB call
    _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "LIMIT",
               qty, limit_price=mid, trade_serial=trade_group.serial_number,
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, mid, outside_rth=True, tif=_session_tif(),
        order_ref=order_ctx.order_ref,
    )

    order_ctx.ib_order_id = str(ib_order_id)

    # PLACE_ACCEPTED — IB returned an order ID
    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
               qty, limit_price=mid, ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type,
               price_placed=mid,
               raw_response=json.dumps({"ib_order_id": ib_order_id, "initial_price": str(mid)}))

    # Register in tracker
    track = ctx.tracker.register(order_ctx.correlation_id, ib_order_id, cmd.symbol)

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
                   ib_responded_at=_now_utc(),
                   trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                   correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
        logger.error(
            '{"event": "ORDER_PLACEMENT_FAILED", "correlation_id": "%s", '
            '"serial": %d, "ib_order_id": "%s", "ib_status": "%s", "reason": "%s"}',
            order_ctx.correlation_id, trade_group.serial_number, ib_order_id, _ib_status, reason,
        )
        raise IBOrderRejectedError(reason)

    if _ib_status == "PreSubmitted":
        # PreSubmitted can be transient — IB may route within milliseconds.
        # Wait up to 3 seconds for it to progress before declaring NOT ACTIVE.
        _PRESUBMIT_SETTLE_SECONDS = 3.0
        _PRESUBMIT_SETTLE_POLL = 0.3
        for _ in range(int(_PRESUBMIT_SETTLE_SECONDS / _PRESUBMIT_SETTLE_POLL)):
            await asyncio.sleep(_PRESUBMIT_SETTLE_POLL)
            _st = await ctx.ib.get_order_status(ib_order_id)
            _ib_status = _st["status"]
            if _ib_status != "PreSubmitted":
                break

        # If it progressed past PreSubmitted, continue to the reprice loop
        if _ib_status in ("Submitted", "Filled"):
            if _ib_status == "Filled":
                # Filled during the settle wait — handle immediately
                _fill_qty = _st.get("qty_filled", Decimal("0"))
                _fill_price = _st.get("avg_fill_price")
                _fill_commission = _st.get("commission") or Decimal("0")
                if _fill_qty > 0 and _fill_price:
                    await _handle_fill(
                        order_ctx, trade_group, _fill_qty, _fill_price,
                        _fill_commission, cmd, con_id, ctx,
                    )
                    ctx.tracker.unregister(ib_order_id)
                    return
            # Submitted — fall through to reprice loop below
            logger.info(
                '{"event": "PRESUBMIT_SETTLED", "ib_order_id": "%s", '
                '"final_status": "%s"}', ib_order_id, _ib_status,
            )
        elif _ib_status == "PreSubmitted":
            # Still PreSubmitted after 3 seconds — handle based on session
            _why_held = _st.get("why_held") or ""
            _expected_reason = presubmitted_reason()
            if not is_ib_session_active():
                # Weekend/break — expected, leave as GTC
                ctx.router.emit(
                    f"\u26a0 QUEUED: {side} {qty} {cmd.symbol} @ ${mid} \u2014 "
                    f"{_expected_reason}\n"
                    f"  GTC order held by IB. No repricing will run.\n"
                    f"  Serial: #{trade_group.serial_number}",
                    pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                    event="ORDER_QUEUED_DISPLAY",
                )
                logger.info(
                    '{"event": "ORDER_QUEUED", "correlation_id": "%s", "serial": %d, '
                    '"symbol": "%s", "price": "%s", "reason": "%s"}',
                    order_ctx.correlation_id, trade_group.serial_number, cmd.symbol, mid, _expected_reason,
                )
                ctx.tracker.unregister(ib_order_id)
                return
            else:
                # Active session, still PreSubmitted after 3s — genuinely stuck
                _detail = f" (IB whyHeld: {_why_held!r})" if _why_held else ""
                reason = (
                    f"Order not working at exchange during {session_label()}"
                    f"{_detail}. IB accepted but did not route."
                )
                await ctx.ib.cancel_order(ib_order_id)
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
                    '{"event": "ORDER_NOT_ROUTED", "correlation_id": "%s", "serial": %d, '
                    '"symbol": "%s", "ib_status": "PreSubmitted", "why_held": "%s"}',
                    order_ctx.correlation_id, trade_group.serial_number, cmd.symbol, _why_held,
                )
                return

    # Start reprice loop
    reprice_task = asyncio.create_task(
        reprice_loop(
            correlation_id=order_ctx.correlation_id,
            ib_order_id=ib_order_id,
            con_id=con_id,
            symbol=cmd.symbol,
            side=side,
            ctx=ctx,
            total_steps=total_steps,
            interval_seconds=float(settings["reprice_interval_seconds"]),
            initial_price=mid,
            trade_id=order_ctx.trade_id,
            leg_type=order_ctx.leg_type,
            security_type=order_ctx.security_type,
            trade_serial=trade_group.serial_number,
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
            await _handle_fill(order_ctx, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
        else:
            await _handle_partial(order_ctx, trade_group, qty, qty_filled, avg_price, commission, cmd, con_id, ib_order_id, ctx)
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
                       trade_serial=trade_group.serial_number,
                       trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                       correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
            await ctx.ib.cancel_order(ib_order_id)
            ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
            _write_txn(ctx, TransactionAction.CANCELLED, cmd.symbol, side, "LIMIT",
                       qty, ib_order_id=_safe_int(ib_order_id),
                       ib_error_message=reason,
                       trade_serial=trade_group.serial_number, is_terminal=True,
                       ib_responded_at=_now_utc(),
                       trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                       correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
            ctx.router.emit(
                f"\u2717 INACTIVE: {qty} {cmd.symbol} \u2014 {reason}\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
                event="ORDER_INACTIVE_DISPLAY",
            )
            logger.error(
                '{"event": "ORDER_INACTIVE", "correlation_id": "%s", "serial": %d, '
                '"symbol": "%s", "reason": "%s", "why_held": "%s"}',
                order_ctx.correlation_id, trade_group.serial_number, cmd.symbol, reason, _why_held,
            )
        else:
            # Normal reprice window expired with no fill.
            # CRITICAL: Cancel-vs-fill race condition.
            # IB may fill the order between our cancel request and the
            # cancel confirmation. We must check for fills AFTER cancelling
            # and before marking the order as expired.
            _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, cmd.symbol, side, "LIMIT",
                       qty, ib_order_id=_safe_int(ib_order_id),
                       trade_serial=trade_group.serial_number,
                       trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                       correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
            await ctx.ib.cancel_order(ib_order_id)

            # Wait briefly for the cancel/fill race to resolve.
            # IB can take several seconds to confirm cancel vs fill.
            _CANCEL_SETTLE_SECONDS = 3.0
            _CANCEL_SETTLE_POLL = 0.3
            settled_as_fill = False
            for _ in range(int(_CANCEL_SETTLE_SECONDS / _CANCEL_SETTLE_POLL)):
                await asyncio.sleep(_CANCEL_SETTLE_POLL)
                status_dict = await ctx.ib.get_order_status(ib_order_id)
                ib_status = status_dict.get("status", "")
                ib_filled = status_dict.get("qty_filled", Decimal("0"))

                if ib_filled and ib_filled > 0:
                    # Fill arrived after cancel — the order actually filled!
                    logger.warning(
                        '{"event": "CANCEL_FILL_RACE_RESOLVED", "ib_order_id": "%s", '
                        '"symbol": "%s", "qty_filled": "%s", "resolution": "filled"}',
                        ib_order_id, cmd.symbol, str(ib_filled),
                    )
                    avg_price = status_dict.get("avg_fill_price") or Decimal("0")
                    commission = status_dict.get("commission") or Decimal("0")
                    await _handle_fill(
                        order_ctx, trade_group, ib_filled, avg_price,
                        commission, cmd, con_id, ctx,
                    )
                    settled_as_fill = True
                    break

                if ib_status in ("Cancelled", "Inactive"):
                    # Cancel confirmed — no fill happened.
                    break

            if not settled_as_fill:
                ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
                _write_txn(ctx, TransactionAction.CANCELLED, cmd.symbol, side, "LIMIT",
                           qty, ib_order_id=_safe_int(ib_order_id),
                           trade_serial=trade_group.serial_number, is_terminal=True,
                           ib_responded_at=_now_utc(),
                           trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                           correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
                ctx.router.emit(
                    f"\u2717 EXPIRED: 0/{qty} filled | reprice window closed "
                    f"({settings['reprice_duration_seconds']}s)\n"
                    f"  Serial: #{trade_group.serial_number}",
                    pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                    event="ORDER_EXPIRED_DISPLAY",
                )
                logger.info(
                    '{"event": "ORDER_EXPIRED", "correlation_id": "%s", "serial": %d, '
                    '"reason": "reprice_timeout_no_fill"}',
                    order_ctx.correlation_id, trade_group.serial_number,
                )

    ctx.tracker.unregister(ib_order_id)


async def _execute_bid_ask_order(
    cmd, order_ctx: _OrderContext, trade_group: TradeGroup, con_id: int,
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
        f"[{_now_display()}] Placed @ ${price} "
        f"(bid: ${bid} ask: ${ask})",
        pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
        event="ORDER_PLACED_BID_ASK",
    )

    _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "LIMIT",
               qty, limit_price=price, trade_serial=trade_group.serial_number,
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, price, outside_rth=True, tif=_session_tif(),
        order_ref=order_ctx.order_ref,
    )

    order_ctx.ib_order_id = str(ib_order_id)

    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
               qty, limit_price=price, ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type,
               price_placed=price,
               raw_response=json.dumps({"ib_order_id": ib_order_id, "price": str(price)}))

    track = ctx.tracker.register(order_ctx.correlation_id, ib_order_id, cmd.symbol)

    # Capture fill-level data from the callback so we don't have to re-read
    # trade.orderStatus, which lags execDetailsEvent. On a fast fill the fill
    # callback runs well before orderStatus.filled / avgFillPrice are updated;
    # re-reading them races and reports qty_filled=0 even though the order
    # actually filled, which then falls through to "PreSubmitted" → NOT_ROUTED.
    _fill_qty: Decimal = Decimal("0")
    _fill_notional: Decimal = Decimal("0")
    _fill_commission: Decimal = Decimal("0")

    async def on_fill(fill_ib_id: str, q: Decimal, avg: Decimal, commission: Decimal):
        nonlocal _fill_qty, _fill_notional, _fill_commission
        if fill_ib_id == ib_order_id:
            _fill_qty += q
            _fill_notional += q * avg
            _fill_commission += commission
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
    # Prefer the fill-callback values (authoritative once fill_event is set).
    # Fall back to trade.orderStatus for the partial / no-fill paths.
    if _fill_qty > 0:
        qty_filled = _fill_qty
        avg_price = (_fill_notional / _fill_qty) if _fill_qty > 0 else None
    else:
        qty_filled = status["qty_filled"]
        avg_price = status["avg_fill_price"]
    commission = _fill_commission if _fill_commission > 0 else (status["commission"] or Decimal("0"))

    if qty_filled > 0 and avg_price is not None:
        if qty_filled >= qty:
            await _handle_fill(order_ctx, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
        else:
            await _handle_partial(order_ctx, trade_group, qty, qty_filled, avg_price, commission, cmd, con_id, ib_order_id, ctx)
    elif track.is_canceled:
        # IB cancelled or set the order Inactive (notify_canceled fires for both).
        # Check actual IB status for the right message and reason.
        _err = ctx.ib.get_order_error(ib_order_id) or ""
        _why = status.get("why_held") or ""
        if ib_status == "Inactive":
            reason = _err or f"IB set order Inactive"
            if _why:
                reason += f" (whyHeld: {_why!r})"
            event_label = "INACTIVE"
        else:
            reason = _err or "IB rejected or cancelled the order"
            event_label = "REJECTED"
        ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
        ctx.router.emit(
            f"\u2717 {event_label}: {qty} {cmd.symbol} \u2014 {reason}\n"
            f"  Serial: #{trade_group.serial_number}",
            pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
            event=f"ORDER_{event_label}_DISPLAY",
        )
        logger.error(
            '{"event": "ORDER_%s", "correlation_id": "%s", "serial": %d, '
            '"symbol": "%s", "reason": "%s"}',
            event_label, order_ctx.correlation_id, trade_group.serial_number, cmd.symbol, reason,
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
                '{"event": "ORDER_QUEUED", "correlation_id": "%s", "serial": %d, '
                '"symbol": "%s", "price": "%s", "reason": "%s"}',
                order_ctx.correlation_id, trade_group.serial_number, cmd.symbol, price, _expected_reason,
            )
        else:
            # Active session — should be Submitted, not PreSubmitted. Cancel it.
            _detail = f" (IB whyHeld: {_why!r})" if _why else ""
            reason = (
                f"Order not working at exchange during {session_label()}"
                f"{_detail}. IB accepted but did not route."
            )
            await ctx.ib.cancel_order(ib_order_id)
            ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
            ctx.router.emit(
                f"\u2717 NOT ACTIVE: {qty} {cmd.symbol} \u2014 {reason}\n"
                f"  Check IB Gateway logs and account permissions.\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
                event="ORDER_PRESUBMITTED_UNEXPECTED_DISPLAY",
            )
            logger.error(
                '{"event": "ORDER_NOT_ROUTED", "correlation_id": "%s", "serial": %d, '
                '"symbol": "%s", "strategy": "%s", "why_held": "%s"}',
                order_ctx.correlation_id, trade_group.serial_number, cmd.symbol, cmd.strategy, _why,
            )
    else:
        # Order live in IB as GTC — leave trade OPEN, daemon will reconcile the fill.
        ctx.router.emit(
            f"\u25cf LIVE: {qty} {cmd.symbol} @ ${price} ({cmd.strategy}) — "
            f"GTC order active in IB\n"
            f"  Serial: #{trade_group.serial_number}",
            pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
            event="ORDER_LIVE_GTC_DISPLAY",
        )
        logger.info(
            '{"event": "ORDER_LIVE_GTC", "correlation_id": "%s", "serial": %d, '
            '"symbol": "%s", "price": "%s", "strategy": "%s"}',
            order_ctx.correlation_id, trade_group.serial_number, cmd.symbol, price, cmd.strategy,
        )

    ctx.tracker.unregister(ib_order_id)


async def _execute_market_order(
    cmd, order_ctx: _OrderContext, trade_group: TradeGroup, con_id: int,
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
                   trade_serial=trade_group.serial_number,
                   trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                   correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
        ib_order_id = await ctx.ib.place_limit_order(
            con_id, cmd.symbol, side, qty, aggressive_price,
            outside_rth=True, tif=_session_tif(),
            order_ref=order_ctx.order_ref,
        )
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
                   qty, limit_price=aggressive_price,
                   ib_order_id=_safe_int(ib_order_id),
                   trade_serial=trade_group.serial_number, ib_responded_at=_now_utc(),
                   trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                   correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type,
                   price_placed=aggressive_price)
    else:
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "MARKET",
                   qty, trade_serial=trade_group.serial_number,
                   trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                   correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
        ib_order_id = await ctx.ib.place_market_order(
            con_id, cmd.symbol, side, qty, outside_rth=True,
            order_ref=order_ctx.order_ref,
        )
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "MARKET",
                   qty, ib_order_id=_safe_int(ib_order_id),
                   trade_serial=trade_group.serial_number, ib_responded_at=_now_utc(),
                   trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                   correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)

    order_ctx.ib_order_id = str(ib_order_id)

    track = ctx.tracker.register(order_ctx.correlation_id, ib_order_id, cmd.symbol)

    # Capture fill data directly from the callback — trade.orderStatus.filled
    # and avgFillPrice lag the execDetailsEvent that fires this callback, so
    # re-reading status races and reports qty_filled=0 on a fast fill.
    _fill_qty: Decimal = Decimal("0")
    _fill_notional: Decimal = Decimal("0")
    _fill_commission: Decimal = Decimal("0")

    async def on_fill(fill_ib_id: str, q: Decimal, avg: Decimal, commission: Decimal):
        nonlocal _fill_qty, _fill_notional, _fill_commission
        if fill_ib_id == ib_order_id:
            _fill_qty += q
            _fill_notional += q * avg
            _fill_commission += commission
            ctx.tracker.notify_filled(fill_ib_id)

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)

    # Market orders should fill quickly — wait up to 30 seconds
    try:
        await asyncio.wait_for(track.fill_event.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        pass

    status = await ctx.ib.get_order_status(ib_order_id)
    if _fill_qty > 0:
        qty_filled = _fill_qty
        avg_price = _fill_notional / _fill_qty
        commission = _fill_commission if _fill_commission > 0 else (status["commission"] or Decimal("0"))
    else:
        qty_filled = status["qty_filled"]
        avg_price = status["avg_fill_price"]
        commission = status["commission"] or Decimal("0")

    if qty_filled >= qty:
        await _handle_fill(order_ctx, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
    elif qty_filled > 0:
        await _handle_partial(order_ctx, trade_group, qty, qty_filled, avg_price, commission, cmd, con_id, ib_order_id, ctx)
    else:
        ctx.router.emit(
            f"\u2717 CANCELED: market order did not fill\n  Serial: #{trade_group.serial_number}",
            pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
            event="ORDER_CANCELED_DISPLAY",
        )

    ctx.tracker.unregister(ib_order_id)


async def _handle_fill(
    order_ctx: _OrderContext, trade_group: TradeGroup, qty_filled: Decimal,
    avg_price: Decimal, commission: Decimal, cmd, con_id: int, ctx: AppContext,
) -> None:
    """Record a complete fill and place profit taker if configured."""
    ctx.trades.update_pnl(trade_group.id, Decimal("0"), commission)

    _write_txn(ctx, TransactionAction.FILLED, order_ctx.symbol, order_ctx.side,
               order_ctx.order_type, qty_filled,
               ib_order_id=_safe_int(order_ctx.ib_order_id),
               ib_status="Filled", ib_filled_qty=qty_filled,
               ib_avg_fill_price=avg_price,
               trade_serial=trade_group.serial_number, is_terminal=True,
               ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type,
               commission=commission)

    ctx.router.emit(
        f"\u2713 FILLED: {qty_filled} shares {order_ctx.symbol} @ ${avg_price} avg\n"
        f"  Commission: ${commission}\n"
        f"  Serial: #{trade_group.serial_number}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
        event="ORDER_FILLED_DISPLAY",
    )
    logger.info(
        '{"event": "ORDER_FILLED", "correlation_id": "%s", "serial": %d, "symbol": "%s", '
        '"qty_filled": "%s", "avg_price": "%s", "commission": "%s"}',
        order_ctx.correlation_id, trade_group.serial_number, order_ctx.symbol, qty_filled, avg_price, commission,
    )

    # Place profit taker if configured
    if cmd.take_profit_price or cmd.profit_amount:
        from ib_trader.repl.commands import BuyCommand as BC
        entry_side = "BUY" if isinstance(cmd, BC) else "SELL"
        await place_profit_taker(
            trade_id=trade_group.id,
            entry_side=entry_side,
            avg_fill_price=avg_price,
            qty_filled=qty_filled,
            profit_amount=cmd.profit_amount,
            take_profit_price=cmd.take_profit_price,
            con_id=con_id,
            symbol=order_ctx.symbol,
            ctx=ctx,
            trade_serial=trade_group.serial_number,
        )


async def _handle_partial(
    order_ctx: _OrderContext, trade_group: TradeGroup, qty_requested: Decimal,
    qty_filled: Decimal, avg_price: Decimal, commission: Decimal,
    cmd, con_id: int, ib_order_id: str, ctx: AppContext,
) -> None:
    """Record a partial fill and cancel the remaining quantity in IB."""
    _write_txn(ctx, TransactionAction.PARTIAL_FILL, order_ctx.symbol, order_ctx.side,
               order_ctx.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               ib_filled_qty=qty_filled, ib_avg_fill_price=avg_price,
               trade_serial=trade_group.serial_number,
               ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type,
               commission=commission)
    _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, order_ctx.symbol, order_ctx.side,
               order_ctx.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number,
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
    await ctx.ib.cancel_order(ib_order_id)
    ctx.trades.update_pnl(trade_group.id, Decimal("0"), commission)
    _write_txn(ctx, TransactionAction.CANCELLED, order_ctx.symbol, order_ctx.side,
               order_ctx.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, is_terminal=True,
               ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)

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
        '{"event": "ORDER_PARTIAL_FILL", "correlation_id": "%s", "serial": %d, '
        '"qty_filled": "%s", "qty_requested": "%s"}',
        order_ctx.correlation_id, trade_group.serial_number, qty_filled, qty_requested,
    )

    if cmd.take_profit_price or cmd.profit_amount:
        from ib_trader.repl.commands import BuyCommand as BC
        entry_side = "BUY" if isinstance(cmd, BC) else "SELL"
        await place_profit_taker(
            trade_id=trade_group.id,
            entry_side=entry_side,
            avg_fill_price=avg_price,
            qty_filled=qty_filled,
            profit_amount=cmd.profit_amount,
            take_profit_price=cmd.take_profit_price,
            con_id=con_id,
            symbol=order_ctx.symbol,
            ctx=ctx,
            trade_serial=trade_group.serial_number,
        )


async def reprice_loop(
    correlation_id: str,
    ib_order_id: str,
    con_id: int,
    symbol: str,
    side: str,
    ctx: AppContext,
    total_steps: int,
    interval_seconds: float,
    initial_price: Decimal,
    trade_id: str | None = None,
    leg_type: LegType | None = None,
    security_type: str | None = None,
    trade_serial: int | None = None,
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
        correlation_id: Correlation ID linking this order's transaction sequence.
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
        trade_id: Trade group UUID (for transaction linking).
        leg_type: Leg type (for transaction linking).
        security_type: Security type (for transaction linking).
        trade_serial: Trade serial number (for transaction linking).
    """
    last_sent_price: Decimal = initial_price

    # Prefer the Redis quote cache (pushed by the engine's pendingTickersEvent
    # handler) — no polling. Fall back to the IB snapshot API only when Redis
    # is unavailable or the latest key is missing.
    state_store = None
    if getattr(ctx, "redis", None) is not None:
        from ib_trader.redis.state import StateStore, StateKeys
        state_store = StateStore(ctx.redis)
        _quote_key = StateKeys.quote_latest(symbol)
    else:
        _quote_key = None

    async def _get_prices() -> tuple[Decimal, Decimal, Decimal]:
        if state_store is not None:
            quote = await state_store.get(_quote_key)
            if quote:
                def _dec(v):
                    if v in (None, "", 0, "0"):
                        return Decimal("0")
                    return Decimal(str(v))
                b = _dec(quote.get("bid"))
                a = _dec(quote.get("ask"))
                l = _dec(quote.get("last"))
                if b > 0 or a > 0 or l > 0:
                    return b, a, l
        snap = await ctx.ib.get_market_snapshot(con_id)
        return snap["bid"], snap["ask"], snap["last"]

    for step in range(1, total_steps + 1):
        track = ctx.tracker.get(ib_order_id)
        if track and (track.is_filled or track.is_canceled):
            break

        await asyncio.sleep(interval_seconds)

        track = ctx.tracker.get(ib_order_id)
        if track and (track.is_filled or track.is_canceled):
            break

        bid, ask, last = await _get_prices()

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
                '{"event": "REPRICE_SKIPPED_SAME_PRICE", "correlation_id": "%s", '
                '"step": %d, "price": "%s"}',
                correlation_id, step, new_price,
            )
            continue

        await ctx.ib.amend_order(ib_order_id, new_price)
        last_sent_price = new_price

        # Write amendment to SQLite
        now = datetime.now(timezone.utc)
        evt = RepriceEvent(
            correlation_id=correlation_id,
            step_number=step,
            bid=bid,
            ask=ask,
            new_price=new_price,
            amendment_confirmed=False,
            timestamp=now,
        )
        evt = ctx.reprice_events.create(evt)

        # Write AMENDED transaction for audit trail
        _write_txn(ctx, TransactionAction.AMENDED, symbol, side, "LIMIT",
                   Decimal("0"), limit_price=new_price,
                   ib_order_id=_safe_int(ib_order_id),
                   trade_serial=trade_serial,
                   trade_id=trade_id, leg_type=leg_type,
                   correlation_id=correlation_id, security_type=security_type,
                   price_placed=new_price)

        # Get current fill status for display
        status = await ctx.ib.get_order_status(ib_order_id)
        qty_filled = status["qty_filled"]

        ctx.router.emit(
            f"[{_now_display()}] Amended \u2192 ${new_price} | "
            f"step {step}/{total_steps} "
            f"(still open: {qty_filled}/? filled)",
            pane=OutputPane.LOG, severity=OutputSeverity.INFO,
            event="REPRICE_STEP_DISPLAY",
        )
        logger.info(
            '{"event": "REPRICE_STEP", "correlation_id": "%s", "step": %d, "total": %d, '
            '"bid": "%s", "ask": "%s", "new_price": "%s"}',
            correlation_id, step, total_steps, bid, ask, new_price,
        )

    logger.info('{"event": "REPRICE_TIMEOUT", "correlation_id": "%s"}', correlation_id)


async def place_profit_taker(
    trade_id: str,
    entry_side: str,
    avg_fill_price: Decimal,
    qty_filled: Decimal,
    profit_amount: Decimal | None,
    take_profit_price: Decimal | None,
    con_id: int,
    symbol: str,
    ctx: AppContext,
    trade_serial: int | None = None,
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
        entry_side: "BUY" or "SELL".
        avg_fill_price: Average fill price of the entry leg.
        qty_filled: Quantity filled on the entry leg.
        profit_amount: Total dollar profit target (or None).
        take_profit_price: Explicit profit taker price (or None).
        con_id: IB contract ID.
        symbol: Ticker symbol.
        ctx: Application context.
        trade_serial: Trade serial number for transaction linking.
    """
    pt_side = "SELL" if entry_side == "BUY" else "BUY"
    pt_correlation_id = str(uuid.uuid4())

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
               qty_filled, limit_price=pt_price,
               trade_id=trade_id, leg_type=LegType.PROFIT_TAKER,
               correlation_id=pt_correlation_id, security_type="STK",
               trade_serial=trade_serial)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, symbol, pt_side, qty_filled, pt_price,
        outside_rth=True, tif=_session_tif(),
    )

    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, symbol, pt_side, "LIMIT",
               qty_filled, limit_price=pt_price, ib_order_id=_safe_int(ib_order_id),
               ib_responded_at=_now_utc(),
               trade_id=trade_id, leg_type=LegType.PROFIT_TAKER,
               correlation_id=pt_correlation_id, security_type="STK",
               price_placed=pt_price, trade_serial=trade_serial)

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
    close_ctx: _OrderContext, trade_group: TradeGroup,
    qty_filled: Decimal, avg_price: Decimal, commission: Decimal,
    ctx: AppContext,
) -> None:
    """Record a complete close fill, compute P&L, and close the trade group."""
    _write_txn(ctx, TransactionAction.FILLED, close_ctx.symbol, close_ctx.side,
               close_ctx.order_type, qty_filled,
               ib_order_id=_safe_int(close_ctx.ib_order_id),
               ib_status="Filled", ib_filled_qty=qty_filled,
               ib_avg_fill_price=avg_price,
               trade_serial=trade_group.serial_number, is_terminal=True,
               ib_responded_at=_now_utc(),
               trade_id=close_ctx.trade_id, leg_type=close_ctx.leg_type,
               correlation_id=close_ctx.correlation_id, security_type=close_ctx.security_type,
               commission=commission)

    # Compute realized P&L: (exit - entry) * qty * direction - commission
    # Get entry data from transactions
    entry_txn = ctx.transactions.get_entry_fill(trade_group.id)
    entry_price = Decimal(str(entry_txn.ib_avg_fill_price)) if entry_txn and entry_txn.ib_avg_fill_price else Decimal("0")
    entry_side = entry_txn.side if entry_txn else ("BUY" if close_ctx.side == "SELL" else "SELL")
    entry_filled_qty = entry_txn.ib_filled_qty or Decimal("0") if entry_txn else Decimal("0")
    direction = Decimal("1") if entry_side == "BUY" else Decimal("-1")
    this_pnl = (avg_price - entry_price) * qty_filled * direction - commission

    # Aggregate with any existing P&L from prior partial closes or profit takers
    existing_pnl = trade_group.realized_pnl or Decimal("0")
    existing_commission = trade_group.total_commission or Decimal("0")
    realized_pnl = existing_pnl + this_pnl
    total_commission = existing_commission + commission

    # Check if the full position is now closed using filled legs from transactions
    filled_legs = ctx.transactions.get_filled_legs(trade_group.id)
    remaining = entry_filled_qty
    for leg in filled_legs:
        if leg.leg_type in (LegType.CLOSE, LegType.PROFIT_TAKER):
            remaining -= leg.ib_filled_qty or Decimal("0")

    ctx.trades.update_pnl(trade_group.id, realized_pnl, total_commission)
    if remaining <= 0:
        ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)

    pnl_str = f"+${realized_pnl}" if realized_pnl >= 0 else f"-${abs(realized_pnl)}"
    closed_label = "CLOSED" if remaining <= 0 else "PARTIAL CLOSE"
    ctx.router.emit(
        f"\u2713 {closed_label}: {qty_filled} shares {close_ctx.symbol} @ ${avg_price}\n"
        f"  P&L: {pnl_str} (commission: ${total_commission})\n"
        f"  Serial: #{trade_group.serial_number}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
        event="CLOSE_ORDER_FILLED",
    )
    logger.info(
        '{"event": "CLOSE_ORDER_FILLED", "correlation_id": "%s", "serial": %d, '
        '"qty_filled": "%s", "avg_price": "%s", "realized_pnl": "%s"}',
        close_ctx.correlation_id, trade_group.serial_number, qty_filled, avg_price, realized_pnl,
    )


async def _handle_close_partial(
    close_ctx: _OrderContext, trade_group: TradeGroup,
    qty_requested: Decimal, qty_filled: Decimal, avg_price: Decimal,
    commission: Decimal, ib_order_id: str, ctx: AppContext,
) -> None:
    """Record a partial close fill and cancel the remaining quantity."""
    await ctx.ib.cancel_order(ib_order_id)

    _write_txn(ctx, TransactionAction.PARTIAL_FILL, close_ctx.symbol, close_ctx.side,
               close_ctx.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               ib_filled_qty=qty_filled, ib_avg_fill_price=avg_price,
               trade_serial=trade_group.serial_number,
               ib_responded_at=_now_utc(),
               trade_id=close_ctx.trade_id, leg_type=close_ctx.leg_type,
               correlation_id=close_ctx.correlation_id, security_type=close_ctx.security_type,
               commission=commission)

    # Get entry data from transactions for P&L calc
    entry_txn = ctx.transactions.get_entry_fill(trade_group.id)
    entry_price = Decimal(str(entry_txn.ib_avg_fill_price)) if entry_txn and entry_txn.ib_avg_fill_price else Decimal("0")
    entry_side = entry_txn.side if entry_txn else ("BUY" if close_ctx.side == "SELL" else "SELL")
    direction = Decimal("1") if entry_side == "BUY" else Decimal("-1")
    this_pnl = (avg_price - entry_price) * qty_filled * direction - commission

    # Aggregate with any existing P&L from prior partial closes
    existing_pnl = trade_group.realized_pnl or Decimal("0")
    existing_commission = trade_group.total_commission or Decimal("0")
    realized_pnl = existing_pnl + this_pnl
    total_commission = existing_commission + commission

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
        '{"event": "CLOSE_ORDER_PARTIAL", "correlation_id": "%s", "serial": %d, '
        '"qty_filled": "%s", "qty_requested": "%s", "realized_pnl": "%s"}',
        close_ctx.correlation_id, trade_group.serial_number, qty_filled, qty_requested, realized_pnl,
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

    # Find entry fill from transactions
    entry_txn = ctx.transactions.get_entry_fill(trade_group.id)
    if not entry_txn or not entry_txn.ib_filled_qty or entry_txn.ib_filled_qty <= 0:
        ctx.router.emit(
            f"\u2717 Error: order #{cmd.serial} has no filled quantity to close",
            pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
        )
        return

    entry_symbol = entry_txn.symbol
    entry_side = entry_txn.side
    qty_to_close = entry_txn.ib_filled_qty

    # Subtract already-filled close legs and profit takers to avoid over-closing
    filled_legs = ctx.transactions.get_filled_legs(trade_group.id)
    for leg in filled_legs:
        if leg.leg_type in (LegType.CLOSE, LegType.PROFIT_TAKER):
            qty_to_close -= leg.ib_filled_qty or Decimal("0")

    if qty_to_close <= 0:
        ctx.router.emit(
            f"\u2717 Warning: trade #{cmd.serial} is already fully closed",
            pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
        )
        return

    # Cancel all linked open orders (profit taker, stop loss)
    open_legs = ctx.transactions.get_open_for_trade(trade_group.id)
    for leg in open_legs:
        if leg.leg_type in (LegType.PROFIT_TAKER, LegType.STOP_LOSS) and leg.ib_order_id:
            _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, leg.symbol, leg.side,
                       leg.order_type, leg.quantity,
                       ib_order_id=_safe_int(leg.ib_order_id),
                       trade_serial=trade_group.serial_number,
                       trade_id=trade_group.id, leg_type=leg.leg_type,
                       correlation_id=leg.correlation_id, security_type=leg.security_type)
            await ctx.ib.cancel_order(leg.ib_order_id)
            _write_txn(ctx, TransactionAction.CANCELLED, leg.symbol, leg.side,
                       leg.order_type, leg.quantity,
                       ib_order_id=_safe_int(leg.ib_order_id),
                       trade_serial=trade_group.serial_number, is_terminal=True,
                       ib_responded_at=_now_utc(),
                       trade_id=trade_group.id, leg_type=leg.leg_type,
                       correlation_id=leg.correlation_id, security_type=leg.security_type)
            logger.info(
                '{"event": "PROFIT_TAKER_CANCELED", "correlation_id": "%s", "reason": "close"}',
                leg.correlation_id,
            )

    # Determine close side (inverse of entry)
    close_side = "SELL" if entry_side == "BUY" else "BUY"

    # Get contract
    contract_info = await _get_contract(entry_symbol, ctx)
    con_id = contract_info["con_id"]

    # Create close _OrderContext
    close_correlation_id = str(uuid.uuid4())
    # Encode orderRef for the close — use 'S' side regardless of underlying
    close_order_ref = None
    if hasattr(cmd, 'bot_ref') and cmd.bot_ref:
        from ib_trader.engine.order_ref import encode as encode_order_ref
        side_code = "S" if close_side == "SELL" else "B"
        close_order_ref = encode_order_ref(cmd.bot_ref, entry_symbol, side_code, cmd.serial)

    close_ctx = _OrderContext(
        trade_id=trade_group.id,
        trade_serial=cmd.serial,
        symbol=entry_symbol,
        side=close_side,
        order_type=cmd.strategy.upper(),
        qty_requested=qty_to_close,
        leg_type=LegType.CLOSE,
        correlation_id=close_correlation_id,
        security_type="STK",
        order_ref=close_order_ref,
    )

    logger.info(
        '{"event": "ORDER_CLOSED_MANUAL", "trade_id": "%s", "serial": %d, '
        '"symbol": "%s", "qty": "%s"}',
        trade_group.id, cmd.serial, entry_symbol, qty_to_close,
    )

    # ── Place IB order (strategy-dependent) ────────────────────────────────
    initial_price = Decimal("0")
    _close_order_type = cmd.strategy.upper() if cmd.strategy != Strategy.MARKET else "MARKET"
    _txn_common = dict(
        trade_id=close_ctx.trade_id, leg_type=close_ctx.leg_type,
        correlation_id=close_ctx.correlation_id, security_type=close_ctx.security_type,
    )
    if cmd.strategy == Strategy.LIMIT:
        initial_price = cmd.limit_price
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   trade_serial=cmd.serial, **_txn_common)
        ib_order_id = await ctx.ib.place_limit_order(
            con_id, entry_symbol, close_side, qty_to_close, initial_price,
            outside_rth=True, tif=_session_tif(),
            order_ref=close_ctx.order_ref,
        )
        close_ctx.ib_order_id = str(ib_order_id)
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   ib_order_id=_safe_int(ib_order_id), trade_serial=cmd.serial,
                   ib_responded_at=_now_utc(), price_placed=initial_price, **_txn_common)
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
                    f"Cannot close {entry_symbol}: no market data available "
                    "(bid=0, ask=0, last=0). Check market data subscription."
                )
            bid = ask = last
        initial_price = calc_mid(bid, ask)
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   trade_serial=cmd.serial, **_txn_common)
        ib_order_id = await ctx.ib.place_limit_order(
            con_id, entry_symbol, close_side, qty_to_close, initial_price,
            outside_rth=True, tif=_session_tif(),
            order_ref=close_ctx.order_ref,
        )
        close_ctx.ib_order_id = str(ib_order_id)
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   ib_order_id=_safe_int(ib_order_id), trade_serial=cmd.serial,
                   ib_responded_at=_now_utc(), price_placed=initial_price, **_txn_common)
        ctx.router.emit(
            f"[{_now_display()}] Close #{cmd.serial} placed "
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
                    f"Cannot close {entry_symbol}: no market data available "
                    "(bid=0, ask=0, last=0). Check market data subscription."
                )
            bid = ask = last
        initial_price = ask if cmd.strategy == Strategy.ASK else bid
        _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   trade_serial=cmd.serial, **_txn_common)
        ib_order_id = await ctx.ib.place_limit_order(
            con_id, entry_symbol, close_side, qty_to_close, initial_price,
            outside_rth=True, tif=_session_tif(),
            order_ref=close_ctx.order_ref,
        )
        close_ctx.ib_order_id = str(ib_order_id)
        _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_symbol, close_side,
                   "LIMIT", qty_to_close, limit_price=initial_price,
                   ib_order_id=_safe_int(ib_order_id), trade_serial=cmd.serial,
                   ib_responded_at=_now_utc(), price_placed=initial_price, **_txn_common)
        ctx.router.emit(
            f"[{_now_display()}] Close #{cmd.serial} placed "
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
                    f"Cannot close {entry_symbol}: no market data available "
                    "(bid=0, ask=0, last=0). Check market data subscription."
                )
            ctx.router.emit(
                f"Overnight session — converting market to limit @ ${initial_price}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                event="MARKET_TO_LIMIT_OVERNIGHT",
            )
            _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_symbol, close_side,
                       "LIMIT", qty_to_close, limit_price=initial_price,
                       trade_serial=cmd.serial, **_txn_common)
            ib_order_id = await ctx.ib.place_limit_order(
                con_id, entry_symbol, close_side, qty_to_close, initial_price,
                outside_rth=True, tif=_session_tif(),
                order_ref=close_ctx.order_ref,
            )
            _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_symbol, close_side,
                       "LIMIT", qty_to_close, limit_price=initial_price,
                       ib_order_id=_safe_int(ib_order_id), trade_serial=cmd.serial,
                       ib_responded_at=_now_utc(), price_placed=initial_price, **_txn_common)
        else:
            _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, entry_symbol, close_side,
                       "MARKET", qty_to_close, trade_serial=cmd.serial, **_txn_common)
            ib_order_id = await ctx.ib.place_market_order(
                con_id, entry_symbol, close_side, qty_to_close, outside_rth=True,
                order_ref=close_ctx.order_ref,
            )
            _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, entry_symbol, close_side,
                       "MARKET", qty_to_close, ib_order_id=_safe_int(ib_order_id),
                       trade_serial=cmd.serial, ib_responded_at=_now_utc(), **_txn_common)
        close_ctx.ib_order_id = str(ib_order_id)
        ctx.router.emit(
            f"Close #{cmd.serial} market order placed.",
            pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
            event="CLOSE_ORDER_PLACED",
        )

    # ── Register tracker + callbacks ─────────────────────────────────────
    track = ctx.tracker.register(close_ctx.correlation_id, ib_order_id, entry_symbol)
    # Authoritative fill values captured by the callback (trade.orderStatus
    # lags execDetailsEvent — see engine/order.py _execute_bid_ask_order fix).
    _fill_qty: Decimal = Decimal("0")
    _fill_notional: Decimal = Decimal("0")
    _fill_commission: Decimal = Decimal("0")

    async def on_fill(fill_ib_id: str, q: Decimal, avg: Decimal, commission: Decimal):
        nonlocal _fill_qty, _fill_notional, _fill_commission
        if fill_ib_id == ib_order_id:
            _fill_qty += q
            _fill_notional += q * avg
            _fill_commission += commission
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
            ctx.tracker.unregister(ib_order_id)
            reason = _ib_err or f"Close order not acknowledged (status: {_ib_status!r})"
            ctx.router.emit(
                f"\u2717 CLOSE FAILED: #{cmd.serial} — {reason}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
                event="CLOSE_ORDER_FAILED",
            )
            logger.error(
                '{"event": "CLOSE_ORDER_FAILED", "correlation_id": "%s", "serial": %d, '
                '"ib_order_id": "%s", "reason": "%s"}',
                close_ctx.correlation_id, cmd.serial, ib_order_id, reason,
            )
            return

    # ── Limit close: fire-and-forget (like buy/sell limit) ──────────────
    if cmd.strategy == Strategy.LIMIT:
        # Check for immediate fill (aggressive limit that crossed the spread)
        status = await ctx.ib.get_order_status(ib_order_id)
        if _fill_qty > 0:
            qty_filled = _fill_qty
            avg_price = _fill_notional / _fill_qty
            commission = _fill_commission if _fill_commission > 0 else (status["commission"] or Decimal("0"))
        else:
            qty_filled = status["qty_filled"]
            avg_price = status["avg_fill_price"]
            commission = status["commission"] or Decimal("0")
        if qty_filled > 0 and avg_price is not None and qty_filled >= qty_to_close:
            await _handle_close_fill(
                close_ctx, trade_group, qty_filled,
                avg_price, commission, ctx,
            )
            ctx.tracker.unregister(ib_order_id)
            return

        # Order is live — return immediately.
        ctx.router.emit(
            f"● CLOSE LIMIT LIVE: #{cmd.serial} {close_side} {qty_to_close} "
            f"{entry_symbol} @ ${initial_price}\n"
            f"  GTC order active in IB  |  IB ID: {ib_order_id}\n"
            f"  Order will persist until filled or manually cancelled.",
            pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS,
            event="CLOSE_LIMIT_LIVE_DISPLAY",
        )
        logger.info(
            '{"event": "CLOSE_LIMIT_LIVE", "correlation_id": "%s", "serial": %d, '
            '"symbol": "%s", "price": "%s", "ib_order_id": "%s"}',
            close_ctx.correlation_id, cmd.serial, entry_symbol, initial_price, ib_order_id,
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
                correlation_id=close_ctx.correlation_id,
                ib_order_id=ib_order_id,
                con_id=con_id,
                symbol=entry_symbol,
                side=close_side,
                ctx=ctx,
                total_steps=total_steps,
                interval_seconds=float(settings["reprice_interval_seconds"]),
                initial_price=initial_price,
                trade_id=close_ctx.trade_id,
                leg_type=close_ctx.leg_type,
                security_type=close_ctx.security_type,
                trade_serial=cmd.serial,
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
    if _fill_qty > 0:
        qty_filled = _fill_qty
        avg_price = _fill_notional / _fill_qty
        commission = _fill_commission if _fill_commission > 0 else (status["commission"] or Decimal("0"))
    else:
        qty_filled = status["qty_filled"]
        avg_price = status["avg_fill_price"]
        commission = status["commission"] or Decimal("0")

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
        if _fill_qty > 0:
            qty_filled = _fill_qty
            avg_price = _fill_notional / _fill_qty
            commission = _fill_commission if _fill_commission > 0 else (status["commission"] or Decimal("0"))
        else:
            qty_filled = status["qty_filled"]
            avg_price = status["avg_fill_price"]
            commission = status["commission"] or Decimal("0")

    if qty_filled > 0 and avg_price is not None:
        if qty_filled >= qty_to_close:
            await _handle_close_fill(
                close_ctx, trade_group, qty_filled,
                avg_price, commission, ctx,
            )
        else:
            await _handle_close_partial(
                close_ctx, trade_group, qty_to_close,
                qty_filled, avg_price, commission, ib_order_id, ctx,
            )
    else:
        # No fill — cancel and check for cancel-vs-fill race.
        await ctx.ib.cancel_order(ib_order_id)

        # Wait briefly for the cancel/fill race to resolve.
        _CANCEL_SETTLE_SECONDS = 3.0
        _CANCEL_SETTLE_POLL = 0.3
        settled_as_fill = False
        for _ in range(int(_CANCEL_SETTLE_SECONDS / _CANCEL_SETTLE_POLL)):
            await asyncio.sleep(_CANCEL_SETTLE_POLL)
            status = await ctx.ib.get_order_status(ib_order_id)
            ib_filled = status.get("qty_filled", Decimal("0"))

            if ib_filled and ib_filled > 0:
                logger.warning(
                    '{"event": "CLOSE_CANCEL_FILL_RACE_RESOLVED", "ib_order_id": "%s", '
                    '"qty_filled": "%s", "resolution": "filled"}',
                    ib_order_id, str(ib_filled),
                )
                avg_price = status.get("avg_fill_price") or Decimal("0")
                commission = status.get("commission") or Decimal("0")
                if ib_filled >= qty_to_close:
                    await _handle_close_fill(
                        close_ctx, trade_group,
                        ib_filled, avg_price, commission, ctx,
                    )
                else:
                    await _handle_close_partial(
                        close_ctx, trade_group, qty_to_close,
                        ib_filled, avg_price, commission, ib_order_id, ctx,
                    )
                settled_as_fill = True
                break

            if status.get("status", "") in ("Cancelled", "Inactive"):
                break

        if not settled_as_fill:
            ctx.router.emit(
                f"\u2717 CLOSE EXPIRED: #{cmd.serial} — 0/{qty_to_close} filled\n"
                f"  Close order timed out. Position remains open.",
                pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                event="CLOSE_ORDER_EXPIRED",
            )
            logger.info(
                '{"event": "CLOSE_ORDER_EXPIRED", "correlation_id": "%s", "serial": %d}',
                close_ctx.correlation_id, cmd.serial,
            )

    ctx.tracker.unregister(ib_order_id)
