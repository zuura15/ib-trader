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
    is_ib_session_active, is_outside_rth, presubmitted_reason, session_label,
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


def _reprice_interval(settings) -> float:
    """Seconds between walker amendments. Derived so it can't drift from
    the active duration / step count."""
    steps = int(settings.get("reprice_steps", 10))
    active = float(settings.get("reprice_active_duration_seconds", 30))
    return active / steps


def _total_order_wait(settings) -> float:
    """Active + passive = the engine's "give up" window for MID / BID /
    ASK / MARKET. Derived to avoid drift across the two phases."""
    return (
        float(settings.get("reprice_active_duration_seconds", 30))
        + float(settings.get("reprice_passive_wait_seconds", 90))
    )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_display() -> str:
    """Local time formatted for user-visible output (not logs/DB — those stay UTC)."""
    return datetime.now().strftime('%H:%M:%S')


def _fmt_qty(q) -> str:
    """Format a quantity for display: whole numbers strip the trailing ``.0``
    so '621.0' shows as '621'. Fractional quantities (future options /
    crypto) keep their decimals."""
    try:
        d = Decimal(str(q))
    except Exception:
        return str(q)
    if d == d.to_integral_value():
        return str(int(d))
    return str(d)


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
        elif cmd.strategy == Strategy.SMART_MARKET:
            await _execute_smart_market_order(cmd, order_ctx, trade_group, con_id, side, qty, ctx)
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
    ctx.router.update_order_row(
        order_ctx.trade_serial, {"ib_order_id": str(ib_order_id)}
    )

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
    ctx.tracker.register(order_ctx.correlation_id, ib_order_id, cmd.symbol)

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
    total_steps = int(settings.get("reprice_steps", 10))

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
    ctx.router.update_order_row(
        order_ctx.trade_serial, {"ib_order_id": str(ib_order_id)}
    )

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
    _fill_running_qty: Decimal = Decimal("0")

    async def on_fill(fill_ib_id: str, _qty: Decimal, _avg: Decimal, commission: Decimal):
        nonlocal _fill_commission, _fill_running_qty
        if fill_ib_id == ib_order_id:
            _fill_commission = commission
            _fill_running_qty += _qty
            ctx.tracker.notify_filled(fill_ib_id)
            ctx.router.emit(
                f"[{_now_display()}] Filled {_fmt_qty(_qty)} @ ${_avg} "
                f"({_fmt_qty(_fill_running_qty)}/{_fmt_qty(qty)})",
                pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                event="ORDER_PARTIAL_FILL_DISPLAY",
            )

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
            interval_seconds=_reprice_interval(settings),
            initial_price=mid,
            target_qty=qty,
            trade_id=order_ctx.trade_id,
            leg_type=order_ctx.leg_type,
            security_type=order_ctx.security_type,
            trade_serial=trade_group.serial_number,
        )
    )

    # Two-phase wait: walker runs for the active duration, then we hold
    # at the last-amended price for the passive duration while IB
    # finishes delivering any residual. `_handle_partial` / cancel paths
    # only fire if the combined window expires with residual unfilled.
    active_duration = float(settings["reprice_active_duration_seconds"])
    await _await_full_fill_or_timeout(
        track, ib_order_id, qty, active_duration + 2, ctx,
    )

    reprice_task.cancel()
    try:
        await reprice_task
    except asyncio.CancelledError:
        pass

    # Passive phase: walker is done. Hold the limit at the last amended
    # price and give IB more time to deliver any residual. Partial-fill
    # callbacks keep updating track.fill_event / qty_filled in the
    # background.
    _status_mid = await ctx.ib.get_order_status(ib_order_id)
    _filled_so_far = _status_mid.get("qty_filled") or Decimal("0")
    if _filled_so_far < qty and not track.is_canceled:
        passive_wait = float(settings["reprice_passive_wait_seconds"])
        if passive_wait > 0:
            ctx.router.emit(
                f"[{_now_display()}] Walker complete \u2014 holding at "
                f"last amended price, waiting up to {int(passive_wait)}s "
                f"for residual\u2026",
                pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                event="ORDER_PASSIVE_WAIT_DISPLAY",
            )
            await _await_full_fill_or_timeout(
                track, ib_order_id, qty, passive_wait, ctx,
            )

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
            reason = _err or "IB set order Inactive"
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
            # Cancel-vs-fill race: IB may fill (or partial-fill) between our
            # cancel request and the cancel confirmation. Emit an "Attempting
            # cancel" status to the user, then let the shared helper wait
            # for IB to pick one outcome.
            ctx.router.emit(
                f"\u27f3 Attempting to cancel #{trade_group.serial_number} "
                f"(no fill within {int(_total_order_wait(settings))}s)\u2026",
                pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                event="ORDER_CANCEL_ATTEMPT_DISPLAY",
            )
            _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, cmd.symbol, side, "LIMIT",
                       qty, ib_order_id=_safe_int(ib_order_id),
                       trade_serial=trade_group.serial_number,
                       trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                       correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)

            settle_timeout = float(ctx.settings.get("cancel_settle_timeout_seconds", 120))
            resolution, final_qty, final_avg, final_commission, _ib_status = \
                await _cancel_and_await_resolution(
                    ctx, ib_order_id, qty, track=track, timeout=settle_timeout,
                    heartbeat_label=f"Cancel pending #{trade_group.serial_number} \u2014",
                )

            if resolution == "filled":
                logger.warning(
                    '{"event": "CANCEL_FILL_RACE_RESOLVED", "ib_order_id": "%s", '
                    '"symbol": "%s", "qty_filled": "%s", "resolution": "filled"}',
                    ib_order_id, cmd.symbol, str(final_qty),
                )
                await _handle_fill(
                    order_ctx, trade_group, final_qty,
                    final_avg or Decimal("0"), final_commission,
                    cmd, con_id, ctx,
                )
            elif final_qty > 0:
                # Late partial fill landed during the cancel settle window.
                logger.warning(
                    '{"event": "CANCEL_FILL_RACE_RESOLVED", "ib_order_id": "%s", '
                    '"symbol": "%s", "qty_filled": "%s", "resolution": "partial"}',
                    ib_order_id, cmd.symbol, str(final_qty),
                )
                _write_txn(ctx, TransactionAction.PARTIAL_FILL, cmd.symbol, side, "LIMIT",
                           qty, ib_order_id=_safe_int(ib_order_id),
                           ib_filled_qty=final_qty, ib_avg_fill_price=final_avg,
                           trade_serial=trade_group.serial_number,
                           ib_responded_at=_now_utc(),
                           trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                           correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type,
                           commission=final_commission)
                # pnl=None — entry partials shouldn't clobber realized_pnl.
                ctx.trades.update_pnl(trade_group.id, None, final_commission)
                _write_txn(ctx, TransactionAction.CANCELLED, cmd.symbol, side, "LIMIT",
                           qty, ib_order_id=_safe_int(ib_order_id),
                           trade_serial=trade_group.serial_number, is_terminal=True,
                           ib_responded_at=_now_utc(),
                           trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                           correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
                ctx.router.emit(
                    f"\u26a0 PARTIAL: {final_qty}/{qty} filled @ avg "
                    f"${final_avg} (cancel beat remainder)\n"
                    f"  Commission: ${final_commission}\n"
                    f"  Serial: #{trade_group.serial_number}",
                    pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                    event="ORDER_PARTIAL_DISPLAY",
                )
            else:
                ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
                _write_txn(ctx, TransactionAction.CANCELLED, cmd.symbol, side, "LIMIT",
                           qty, ib_order_id=_safe_int(ib_order_id),
                           trade_serial=trade_group.serial_number, is_terminal=True,
                           ib_responded_at=_now_utc(),
                           trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                           correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)
                cancel_note = "cancel confirmed" if resolution == "cancelled" else "cancel ack timeout"
                ctx.router.emit(
                    f"\u2717 EXPIRED: 0/{qty} filled | order window closed "
                    f"({int(_total_order_wait(settings))}s, {cancel_note})\n"
                    f"  Serial: #{trade_group.serial_number}",
                    pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                    event="ORDER_EXPIRED_DISPLAY",
                )
                logger.info(
                    '{"event": "ORDER_EXPIRED", "correlation_id": "%s", "serial": %d, '
                    '"reason": "reprice_timeout_no_fill", "cancel_resolution": "%s"}',
                    order_ctx.correlation_id, trade_group.serial_number, resolution,
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
    ctx.router.update_order_row(
        order_ctx.trade_serial, {"ib_order_id": str(ib_order_id)}
    )

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
            ctx.router.emit(
                f"[{_now_display()}] Filled {_fmt_qty(q)} @ ${avg} "
                f"({_fmt_qty(_fill_qty)}/{_fmt_qty(qty)})",
                pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                event="ORDER_PARTIAL_FILL_DISPLAY",
            )

    async def on_status(status_ib_id: str, status: str):
        if status_ib_id == ib_order_id and status in ("Cancelled", "Inactive"):
            ctx.tracker.notify_canceled(status_ib_id)

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)
    ctx.ib.register_status_callback(on_status, ib_order_id=ib_order_id)

    # Wait for the full give-up window (active + passive). BID/ASK has no
    # walker, so the whole interval is effectively passive: place at the
    # quote and wait for IB. Partial-aware waiter loops across split fills.
    # If still unfilled at the end, the GTC order stays live in IB and the
    # daemon reconciler picks up any late fill.
    await _await_full_fill_or_timeout(
        track, ib_order_id, qty, _total_order_wait(ctx.settings), ctx,
    )

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
            reason = _err or "IB set order Inactive"
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


def _slippage_floor(trigger_price: Decimal, side: str, max_slip_pct: Decimal) -> Decimal:
    """Compute the worst price we're willing to walk to on SMART_MARKET
    during ETH. For SELL this is the lowest (trigger × (1 - pct)); for
    BUY it's the highest (trigger × (1 + pct))."""
    if side == "SELL":
        return (trigger_price * (Decimal("1") - max_slip_pct)).quantize(Decimal("0.01"))
    return (trigger_price * (Decimal("1") + max_slip_pct)).quantize(Decimal("0.01"))


def _apply_cap(step_price: Decimal, floor_price: Decimal, side: str) -> Decimal:
    """Clamp a proposed step price to the slippage floor. Returns the
    more conservative of the two so we never cross the cap."""
    if side == "SELL":
        # SELL floor is the LOWEST price we're willing to accept —
        # clamp step_price up to floor_price if it's below.
        return max(step_price, floor_price)
    # BUY floor is the HIGHEST — clamp step_price down to floor.
    return min(step_price, floor_price)


async def _raise_eth_cap_alert(
    ctx: AppContext, symbol: str, side: str, trigger_price: Decimal,
    floor_price: Decimal, residual_qty: Decimal, ib_order_id: str,
    trade_group: TradeGroup,
) -> None:
    """Raise CATASTROPHIC alert when SMART_MARKET ETH walker hits the
    slippage cap. Writes to ``alerts:active`` + nudges the WS channel.
    The order is left resting at the floor for human resolution."""
    import uuid as _uuid
    alert_id = str(_uuid.uuid4())
    alert_dict = {
        "id": alert_id,
        "severity": "CATASTROPHIC",
        "trigger": "EXIT_PRICE_CAP_REACHED",
        "message": (
            f"SMART_MARKET {side} for {residual_qty} {symbol} reached its "
            f"slippage floor (${floor_price}) without filling. Order is "
            f"resting at IB — manual intervention required."
        ),
        "symbol": symbol,
        "side": side,
        "trigger_price": str(trigger_price),
        "floor_price": str(floor_price),
        "residual_qty": str(residual_qty),
        "ib_order_id": ib_order_id,
        "trade_serial": trade_group.serial_number,
        "created_at": _now_utc().isoformat(),
        "pager": True,
    }
    redis = getattr(ctx, "redis", None)
    if redis is None:
        logger.error(
            '{"event": "EXIT_PRICE_CAP_REACHED_NO_REDIS", "symbol": "%s", '
            '"ib_order_id": "%s"}',
            symbol, ib_order_id,
        )
        return
    try:
        from ib_trader.redis.state import StateKeys
        await StateKeys.publish_alert(redis, alert_id, alert_dict)
        from ib_trader.redis.streams import publish_activity
        await publish_activity(redis, "alerts")
    except Exception:
        logger.exception(
            '{"event": "EXIT_PRICE_CAP_ALERT_PUBLISH_FAILED", "symbol": "%s"}',
            symbol,
        )
    ctx.router.emit(
        f"\u26a0 EXIT CAP REACHED: {side} {residual_qty} {symbol} @ ${floor_price} — "
        f"order resting at IB, manual intervention required.\n"
        f"  Serial: #{trade_group.serial_number}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR,
        event="EXIT_PRICE_CAP_REACHED_DISPLAY",
    )
    logger.error(
        '{"event": "EXIT_PRICE_CAP_REACHED", "symbol": "%s", "side": "%s", '
        '"trigger_price": "%s", "floor_price": "%s", "residual_qty": "%s", '
        '"ib_order_id": "%s", "trade_serial": %d}',
        symbol, side, trigger_price, floor_price, residual_qty,
        ib_order_id, trade_group.serial_number,
    )


async def _walk_limit_aggressive(
    ctx: AppContext, con_id: int, ib_order_id: str, symbol: str, side: str,
    trigger_price: Decimal, interval_seconds: float,
    total_duration_seconds: float | None, floor_price: Decimal | None,
    target_qty: Decimal,
) -> dict:
    """Reprice loop for SMART_MARKET.

    Every ``interval_seconds`` (default ~100 ms) we read the current
    mid and step the limit aggressively toward the far side (bid for
    SELL, ask for BUY). Exits early on:
      - track.fill_event set (filled or cancelled)
      - full fill observed in order status
      - ``total_duration_seconds`` elapsed (RTH)
      - ``floor_price`` reached (ETH)

    Returns a dict describing how we exited: ``{status, hit_cap,
    last_sent_price, filled_qty}``.
    """
    from ib_trader.redis.state import StateKeys as _SK
    loop = asyncio.get_event_loop()
    deadline = (
        loop.time() + total_duration_seconds
        if total_duration_seconds is not None else None
    )
    track = ctx.tracker.get(ib_order_id)
    last_sent = trigger_price

    redis = getattr(ctx, "redis", None)
    state_store = None
    _quote_key = None
    if redis is not None:
        from ib_trader.redis.state import StateStore
        state_store = StateStore(redis)
        _quote_key = _SK.quote_latest(symbol)

    async def _get_prices() -> tuple[Decimal, Decimal, Decimal]:
        if state_store is not None and _quote_key is not None:
            q = await state_store.get(_quote_key)
            if q:
                def _d(v):
                    if v in (None, "", 0, "0"):
                        return Decimal("0")
                    return Decimal(str(v))
                b, a, l = _d(q.get("bid")), _d(q.get("ask")), _d(q.get("last"))
                if b > 0 or a > 0 or l > 0:
                    return b, a, l
        snap = await ctx.ib.get_market_snapshot(con_id)
        return snap["bid"], snap["ask"], snap["last"]

    while True:
        if track and (track.is_filled or track.is_canceled):
            return {"status": "filled_or_canceled", "hit_cap": False,
                    "last_sent_price": last_sent}
        if deadline is not None and loop.time() >= deadline:
            return {"status": "duration_expired", "hit_cap": False,
                    "last_sent_price": last_sent}

        # Check current fill status — if IB filled during our nap, exit
        # without another amend (amend on a filled order would error).
        st = await ctx.ib.get_order_status(ib_order_id)
        if (st.get("qty_filled") or Decimal("0")) >= target_qty:
            return {"status": "filled", "hit_cap": False,
                    "last_sent_price": last_sent}
        if st.get("status") in ("Cancelled", "Inactive"):
            return {"status": "cancelled", "hit_cap": False,
                    "last_sent_price": last_sent}

        # Sleep either until the next interval tick or the deadline,
        # whichever is sooner. Wake immediately on a fill/cancel event.
        wait = interval_seconds
        if deadline is not None:
            wait = min(wait, max(deadline - loop.time(), 0.0))
        if track is not None:
            try:
                await asyncio.wait_for(track.fill_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(wait)

        # Fetch quote, compute next step. We move the limit one IB tick
        # toward the far side per iteration (or chase the quote if it's
        # already beyond us). Floor clamps the furthest we'll go; the
        # walker returns ``hit_cap=True`` once it's sitting at the floor.
        bid, ask, last = await _get_prices()
        if bid == 0 and ask == 0:
            if last > 0:
                bid = ask = last
            else:
                # No data — skip this tick rather than amend blindly.
                continue
        tick = Decimal("0.01")
        if side == "SELL":
            step_price = last_sent - tick
            # If the market moved below us, chase it down in one jump.
            if bid > 0 and bid < step_price:
                step_price = bid
        else:  # BUY
            step_price = last_sent + tick
            if ask > 0 and ask > step_price:
                step_price = ask

        if floor_price is not None:
            step_price = _apply_cap(step_price, floor_price, side)
            # We're "at the floor" when clamping left us exactly at
            # floor_price AND we're already sitting there.
            at_floor = (
                step_price == floor_price
                and last_sent == floor_price
            )
            if at_floor:
                return {"status": "cap_reached", "hit_cap": True,
                        "last_sent_price": last_sent}

        step_price = step_price.quantize(Decimal("0.01"))
        if step_price == last_sent:
            continue

        # Re-check order state immediately before amending. The top-of-loop
        # check goes stale across the asyncio.wait_for + quote-fetch window
        # (tens to hundreds of ms), during which IB can fill/cancel the
        # order. Amending a terminal order triggers IB error 104 / 201 and
        # an ib_async AssertionError — skip the amend entirely instead.
        if track is not None and (track.is_filled or track.is_canceled):
            return {
                "status": "filled_or_canceled",
                "hit_cap": False,
                "last_sent_price": last_sent,
            }
        try:
            _pre = await ctx.ib.get_order_status(ib_order_id)
        except Exception:
            _pre = {"status": "", "qty_filled": Decimal("0")}
        _pre_st = _pre.get("status") or ""
        if _pre_st in ("Filled", "Cancelled", "Inactive", "ApiCancelled"):
            return {
                "status": _pre_st.lower(),
                "hit_cap": False,
                "last_sent_price": last_sent,
            }
        if (_pre.get("qty_filled") or Decimal("0")) >= target_qty:
            return {
                "status": "filled",
                "hit_cap": False,
                "last_sent_price": last_sent,
            }

        try:
            await ctx.ib.amend_order(ib_order_id, step_price)
            last_sent = step_price
        except Exception as amend_exc:
            # Residual race — pre-check was clean but the order
            # terminalised in the microseconds between our check and IB
            # receiving the amend. Verify and return without looping. The
            # two terminal branches below log at WARNING because this is
            # the expected tail of the race window; only the "non-terminal
            # but amend failed" branch remains ERROR-worthy.
            try:
                _check = await ctx.ib.get_order_status(ib_order_id)
            except Exception:
                _check = {"status": "", "qty_filled": Decimal("0")}
            _st = _check.get("status") or ""
            _qf = _check.get("qty_filled") or Decimal("0")
            if _st in ("Filled", "Cancelled", "Inactive", "ApiCancelled") or _qf >= target_qty:
                logger.warning(
                    '{"event": "SMART_MARKET_AMEND_RACE", "ib_order_id": "%s", '
                    '"status": "%s", "qty_filled": "%s"}',
                    ib_order_id, _st, str(_qf),
                )
                return {
                    "status": _st.lower() if _st else "filled",
                    "hit_cap": False,
                    "last_sent_price": last_sent,
                }
            # Order not terminal but amend failed — unexpected. Log full
            # stack trace so we notice, and abort the walker rather than
            # spin on a permanent failure mode.
            logger.error(
                '{"event": "SMART_MARKET_AMEND_FAILED", "ib_order_id": "%s", '
                '"status": "%s", "qty_filled": "%s"}',
                ib_order_id, _st, str(_qf),
                exc_info=amend_exc,
            )
            return {
                "status": "amend_failed",
                "hit_cap": False,
                "last_sent_price": last_sent,
            }


async def _execute_smart_market_order(
    cmd, order_ctx: _OrderContext, trade_group: TradeGroup, con_id: int,
    side: str, qty: Decimal, ctx: AppContext,
) -> None:
    """Session-aware aggressive-mid execution.

    RTH: aggressive walker for ``smart_market_rth_duration_seconds``,
    then cross to MKT for any residual.
    ETH: aggressive walker with no time limit but capped at
    ``trigger_price × (1 ± max_slippage_pct)``; raises CATASTROPHIC
    alert if the cap is hit.
    """
    settings = ctx.settings
    interval = float(settings.get("smart_market_reprice_interval_ms", 100)) / 1000.0
    rth_duration = float(settings.get("smart_market_rth_duration_seconds", 10))
    max_slip = Decimal(str(settings.get("smart_market_eth_max_slippage_pct", 0.005)))

    snapshot = await ctx.ib.get_market_snapshot(con_id)
    bid, ask, last = snapshot["bid"], snapshot["ask"], snapshot["last"]
    if bid == 0 and ask == 0:
        if last == 0:
            raise ValueError(
                f"Cannot place SMART_MARKET for {cmd.symbol}: no market data "
                "available (bid=0, ask=0, last=0)."
            )
        bid = ask = last
    trigger_price = calc_mid(bid, ask)
    floor_price = _slippage_floor(trigger_price, side, max_slip)
    rth = not is_outside_rth()

    ctx.router.emit(
        f"Order #{trade_group.serial_number} \u2014 {side} {qty} {cmd.symbol} @ smart_market\n"
        f"[{_now_display()}] Placed @ ${trigger_price} "
        f"(bid: ${bid} ask: ${ask}) session={'RTH' if rth else 'ETH'}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
        event="ORDER_PLACED_SMART_MARKET",
    )

    # 1. Place initial LMT at mid.
    _write_txn(ctx, TransactionAction.PLACE_ATTEMPT, cmd.symbol, side, "LIMIT",
               qty, limit_price=trigger_price,
               trade_serial=trade_group.serial_number,
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id,
               security_type=order_ctx.security_type)

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, trigger_price,
        outside_rth=True, tif=_session_tif(),
        order_ref=order_ctx.order_ref,
    )
    order_ctx.ib_order_id = str(ib_order_id)
    ctx.router.update_order_row(
        order_ctx.trade_serial, {"ib_order_id": str(ib_order_id)}
    )
    _write_txn(ctx, TransactionAction.PLACE_ACCEPTED, cmd.symbol, side, "LIMIT",
               qty, limit_price=trigger_price, ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number,
               ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id,
               security_type=order_ctx.security_type,
               price_placed=trigger_price)

    track = ctx.tracker.register(order_ctx.correlation_id, ib_order_id, cmd.symbol)
    _fill_commission: Decimal | None = None

    async def on_fill(fill_ib_id: str, _q: Decimal, _a: Decimal, commission: Decimal):
        nonlocal _fill_commission
        if fill_ib_id == ib_order_id:
            _fill_commission = commission
            ctx.tracker.notify_filled(fill_ib_id)

    async def on_status(status_ib_id: str, status: str):
        if status_ib_id == ib_order_id and status in ("Cancelled", "Inactive"):
            ctx.tracker.notify_canceled(status_ib_id)

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)
    ctx.ib.register_status_callback(on_status, ib_order_id=ib_order_id)

    # 2. Walk toward the far side.
    walk = await _walk_limit_aggressive(
        ctx, con_id, ib_order_id, cmd.symbol, side, trigger_price,
        interval_seconds=interval,
        total_duration_seconds=rth_duration if rth else None,
        floor_price=None if rth else floor_price,
        target_qty=qty,
    )

    # 3. Post-walk decision.
    status = await ctx.ib.get_order_status(ib_order_id)
    qty_filled = status.get("qty_filled") or Decimal("0")
    avg_price = status.get("avg_fill_price")
    commission = _fill_commission if _fill_commission is not None else (
        status.get("commission") or Decimal("0")
    )

    # Fully filled during the walk — happy path.
    if qty_filled >= qty and avg_price is not None:
        await _handle_fill(order_ctx, trade_group, qty_filled, avg_price,
                           commission, cmd, con_id, ctx)
        ctx.tracker.unregister(ib_order_id)
        return

    # ETH cap reached — leave the order resting, alert, return.
    if walk.get("hit_cap"):
        residual = qty - qty_filled
        await _raise_eth_cap_alert(
            ctx, cmd.symbol, side, trigger_price, floor_price, residual,
            ib_order_id, trade_group,
        )
        # Do NOT unregister — callbacks stay so a late fill can still be recorded.
        return

    # RTH duration expired with residual — cancel the working limit, then
    # submit a MKT for whatever's still outstanding.
    if rth and walk.get("status") == "duration_expired":
        ctx.router.emit(
            f"\u27f3 SMART_MARKET: walker expired, crossing to MKT for residual "
            f"({qty - qty_filled} of {qty})...",
            pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
            event="SMART_MARKET_CROSS_TO_MARKET",
        )
        settle_timeout = float(settings.get("cancel_settle_timeout_seconds", 120))
        resolution, final_qty, final_avg, final_comm, _ = \
            await _cancel_and_await_resolution(
                ctx, ib_order_id, qty, track=track, timeout=settle_timeout,
                heartbeat_label=f"Cancel pending #{trade_group.serial_number} \u2014",
            )
        if final_comm > commission:
            commission = final_comm
        effective_avg = final_avg if final_avg is not None else avg_price

        if final_qty >= qty:
            await _handle_fill(order_ctx, trade_group, final_qty,
                               effective_avg or trigger_price, commission,
                               cmd, con_id, ctx)
            ctx.tracker.unregister(ib_order_id)
            return

        residual = qty - final_qty
        if residual <= 0:
            ctx.tracker.unregister(ib_order_id)
            return

        # Submit the MKT for the remainder. Reuses _execute_market_order's
        # place-market-order helper indirectly via ctx.ib.place_market_order.
        try:
            mkt_order_id = await ctx.ib.place_market_order(
                con_id, cmd.symbol, side, residual,
                outside_rth=True, order_ref=order_ctx.order_ref,
            )
        except Exception as e:
            logger.exception(
                '{"event": "SMART_MARKET_MKT_TERMINAL_FAILED", "symbol": "%s", "error": "%s"}',
                cmd.symbol, str(e),
            )
            # Best we can do: record what we got so far as partial, leave
            # the rest for operator attention.
            if final_qty > 0 and effective_avg is not None:
                await _handle_partial(
                    order_ctx, trade_group, qty, final_qty,
                    effective_avg, commission, cmd, con_id, ib_order_id, ctx,
                )
            ctx.tracker.unregister(ib_order_id)
            raise

        # Track & wait for the MKT to settle.
        mkt_track = ctx.tracker.register(order_ctx.correlation_id, mkt_order_id, cmd.symbol)
        _mkt_commission: Decimal | None = None

        async def mkt_on_fill(fill_id: str, _q: Decimal, _a: Decimal, c: Decimal):
            nonlocal _mkt_commission
            if fill_id == mkt_order_id:
                _mkt_commission = c
                ctx.tracker.notify_filled(fill_id)

        async def mkt_on_status(sid: str, st: str):
            if sid == mkt_order_id and st in ("Cancelled", "Inactive"):
                ctx.tracker.notify_canceled(sid)

        ctx.ib.register_fill_callback(mkt_on_fill, ib_order_id=mkt_order_id)
        ctx.ib.register_status_callback(mkt_on_status, ib_order_id=mkt_order_id)

        await _await_full_fill_or_timeout(
            mkt_track, mkt_order_id, residual,
            float(settings.get("market_order_wait_seconds", 30)), ctx,
        )
        mkt_status = await ctx.ib.get_order_status(mkt_order_id)
        mkt_filled = mkt_status.get("qty_filled") or Decimal("0")
        mkt_avg = mkt_status.get("avg_fill_price")
        mkt_comm = _mkt_commission if _mkt_commission is not None else (
            mkt_status.get("commission") or Decimal("0")
        )

        # Combine the limit fills + the market fills into a single
        # aggregate for the terminal txn.
        total_filled = final_qty + mkt_filled
        if total_filled > 0:
            # Weighted average across both legs, protecting against missing avg.
            num = Decimal("0")
            denom = Decimal("0")
            if final_qty > 0 and effective_avg is not None:
                num += effective_avg * final_qty
                denom += final_qty
            if mkt_filled > 0 and mkt_avg is not None:
                num += mkt_avg * mkt_filled
                denom += mkt_filled
            agg_avg = (num / denom).quantize(Decimal("0.01")) if denom > 0 else trigger_price
            agg_comm = commission + mkt_comm
            if total_filled >= qty:
                await _handle_fill(order_ctx, trade_group, total_filled,
                                   agg_avg, agg_comm, cmd, con_id, ctx)
            else:
                await _handle_partial(order_ctx, trade_group, qty, total_filled,
                                      agg_avg, agg_comm, cmd, con_id, mkt_order_id, ctx)
        else:
            # Zero fills across both legs — same EXPIRED semantics as mid
            # when nothing took. Write terminal CANCELLED and unregister.
            ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
            _write_txn(ctx, TransactionAction.CANCELLED, cmd.symbol, side, "LIMIT",
                       qty, ib_order_id=_safe_int(ib_order_id),
                       trade_serial=trade_group.serial_number, is_terminal=True,
                       ib_responded_at=_now_utc(),
                       trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                       correlation_id=order_ctx.correlation_id,
                       security_type=order_ctx.security_type)
            ctx.router.emit(
                f"\u2717 EXPIRED: 0/{qty} filled (smart_market walker + MKT terminal)\n"
                f"  Serial: #{trade_group.serial_number}",
                pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
                event="ORDER_EXPIRED_DISPLAY",
            )

        ctx.tracker.unregister(mkt_order_id)
        ctx.tracker.unregister(ib_order_id)
        return

    # Any other exit path (e.g. walker exited due to external cancel):
    # fall through to the standard partial / cancelled handling.
    if qty_filled > 0 and avg_price is not None:
        await _handle_partial(order_ctx, trade_group, qty, qty_filled,
                              avg_price, commission, cmd, con_id, ib_order_id, ctx)
    else:
        ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
        _write_txn(ctx, TransactionAction.CANCELLED, cmd.symbol, side, "LIMIT",
                   qty, ib_order_id=_safe_int(ib_order_id),
                   trade_serial=trade_group.serial_number, is_terminal=True,
                   ib_responded_at=_now_utc(),
                   trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
                   correlation_id=order_ctx.correlation_id,
                   security_type=order_ctx.security_type)
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
    if is_outside_rth():
        # Outside RTH: exchanges reject market orders — convert to aggressive limit
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
    ctx.router.update_order_row(
        order_ctx.trade_serial, {"ib_order_id": str(ib_order_id)}
    )

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
            ctx.router.emit(
                f"[{_now_display()}] Filled {_fmt_qty(q)} @ ${avg} "
                f"({_fmt_qty(_fill_qty)}/{_fmt_qty(qty)})",
                pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                event="ORDER_PARTIAL_FILL_DISPLAY",
            )

    ctx.ib.register_fill_callback(on_fill, ib_order_id=ib_order_id)

    # Market orders should fill quickly — wait up to the unified total
    # order window (active + passive). Partial-aware waiter so a SMART
    # split (fills arriving as 9 + 6, etc.) doesn't cause us to declare
    # PARTIAL while the remainder is still en route.
    await _await_full_fill_or_timeout(
        track, ib_order_id, qty, _total_order_wait(ctx.settings), ctx,
    )

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
    # pnl=None preserves the trade_group's realized_pnl (stays NULL for
    # entries; the close path owns P&L writes). Previously we wrote
    # Decimal("0") here which clobbered bot-driven trade_groups with a
    # meaningless zero and made the Trades panel read "$0.00" for every
    # row. Commission still updates normally.
    ctx.trades.update_pnl(trade_group.id, None, commission)

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
        f"\u2713 FILLED: {_fmt_qty(qty_filled)} shares {order_ctx.symbol} @ ${avg_price} avg\n"
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


async def _await_full_fill_or_timeout(
    track, ib_order_id: str, target_qty: Decimal,
    timeout: float, ctx: AppContext,
) -> None:
    """Wait for cumulative fill >= target_qty, cancel, or timeout.

    ``tracker.notify_filled`` sets ``fill_event`` on EVERY partial fill, so
    a naive ``await wait_for(fill_event.wait(), ...)`` returns on the first
    partial and causes the caller to declare PARTIAL + cancel while IB is
    still filling the remainder (live SMART routing frequently splits an
    order across venues and reports fills incrementally). We instead loop:
    wait, clear, re-check cumulative filled qty, and keep waiting until
    full fill, cancel, or the window elapses.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(track.fill_event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return
        track.fill_event.clear()
        if track.is_canceled:
            return
        status = await ctx.ib.get_order_status(ib_order_id)
        if status["qty_filled"] >= target_qty:
            return


async def _cancel_and_await_resolution(
    ctx: AppContext, ib_order_id: str, target_qty: Decimal,
    track=None, timeout: float = 10.0,
    heartbeat_seconds: float = 5.0,
    heartbeat_label: str = "",
) -> tuple[str, Decimal, Decimal | None, Decimal, str]:
    """Send a cancel and wait for IB to settle the cancel-vs-fill race.

    IB's cancel is not instant: the order may already be at the exchange
    being filled when we send the cancel. Outcomes we need to distinguish:
      - the remainder fills before our cancel lands (promote to FILLED)
      - IB confirms the cancel (genuine PARTIAL / EXPIRED)
      - IB is slow to tell us either way (timeout)

    Polls ``get_order_status`` on every event-wake (fill or status change)
    plus a ~0.5 s fallback tick so a missed event can't strand us.

    Returns ``(resolution, qty_filled, avg_fill_price, commission, ib_status)``
    where ``resolution`` is one of ``"filled"``, ``"cancelled"``, ``"timeout"``.
    """
    # Dispatch the cancel under asyncio.shield so a client-side timeout
    # on the caller (e.g. the API server's httpx client giving up on us)
    # can't abort the IB cancel mid-throttle and leave the order live.
    # If the outer task is cancelled while we're inside the throttle's
    # asyncio.sleep, the shielded cancel continues to completion; the
    # CancelledError is re-raised afterwards so the enclosing wait unwinds.
    cancel_task = asyncio.ensure_future(ctx.ib.cancel_order(ib_order_id))
    try:
        await asyncio.shield(cancel_task)
    except asyncio.CancelledError:
        # Outer task cancelled. Let the cancel dispatch finish in the
        # background — IB will still see the cancelOrder packet.
        raise
    except Exception:
        logger.exception(
            '{"event": "CANCEL_SUBMIT_FAILED", "ib_order_id": "%s"}', ib_order_id,
        )

    # Primary path: ib_async pushes orderStatus / execDetails callbacks,
    # which set ``track.fill_event`` — the wait below returns immediately
    # on any state change. ``get_order_status`` reads the locally cached
    # ``trade.orderStatus`` so it's essentially free.
    #
    # Backstop: every ``_RESYNC_INTERVAL_S`` we call ``get_open_orders``
    # (which fires ``reqAllOpenOrdersAsync`` under the hood). This forces
    # IB to re-push full state for every live order — if a status or
    # fill callback was ever dropped, this is where we'd pick it up.
    _RESYNC_INTERVAL_S = 10.0
    _RESYNC_CALL_TIMEOUT_S = 5.0
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_resync = loop.time()
    last_heartbeat = loop.time()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        # User-visible heartbeat so the command feed doesn't go silent
        # while we wait for IB to confirm the cancel (can be 30s+ on
        # overnight venues). Also keeps the WS consumer's stream warm.
        if heartbeat_seconds > 0 and loop.time() - last_heartbeat >= heartbeat_seconds:
            try:
                _hb_status = await ctx.ib.get_order_status(ib_order_id)
                _hb_filled = _hb_status.get("qty_filled") or Decimal("0")
                prefix = f"{heartbeat_label} " if heartbeat_label else ""
                ctx.router.emit(
                    f"[{_now_display()}] {prefix}waiting for IB... "
                    f"({_fmt_qty(_hb_filled)}/{_fmt_qty(target_qty)} filled)",
                    pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                    event="CANCEL_SETTLE_HEARTBEAT_DISPLAY",
                )
            except Exception:
                logger.debug(
                    "cancel-settle heartbeat emit failed", exc_info=True,
                )
            last_heartbeat = loop.time()
        if loop.time() - last_resync >= _RESYNC_INTERVAL_S:
            # Bound the resync call — if IB is in a weird state (e.g. IBEOS
            # is mid-cancel and reqAllOpenOrdersAsync never returns) we must
            # not block the waiter indefinitely. Skip this cycle and try
            # again on the next interval.
            try:
                await asyncio.wait_for(
                    ctx.ib.get_open_orders(),
                    timeout=_RESYNC_CALL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    '{"event": "CANCEL_SETTLE_RESYNC_TIMEOUT", '
                    '"ib_order_id": "%s", "timeout_s": %.1f}',
                    ib_order_id, _RESYNC_CALL_TIMEOUT_S,
                )
            except Exception:
                logger.debug(
                    "cancel-settle resync failed", exc_info=True,
                )
            last_resync = loop.time()
        if track is not None:
            try:
                await asyncio.wait_for(
                    track.fill_event.wait(), timeout=min(0.5, remaining),
                )
            except asyncio.TimeoutError:
                pass
            track.fill_event.clear()
        else:
            await asyncio.sleep(min(0.3, remaining))

        status = await ctx.ib.get_order_status(ib_order_id)
        filled = status.get("qty_filled") or Decimal("0")
        ib_status = status.get("status") or ""
        if filled >= target_qty:
            return (
                "filled", filled,
                status.get("avg_fill_price"),
                status.get("commission") or Decimal("0"),
                ib_status,
            )
        if ib_status in ("Cancelled", "Inactive"):
            return (
                "cancelled", filled,
                status.get("avg_fill_price"),
                status.get("commission") or Decimal("0"),
                ib_status,
            )
        # Also treat our tracker's cancel-notified flag as terminal. IBEOS
        # sometimes flips status Cancelled → Submitted → Filled across tens
        # of milliseconds; the status read above can land on a post-flip
        # "Submitted" and miss the Cancelled window entirely. track.is_canceled
        # is set the moment the status callback saw "Cancelled", so it's a
        # more reliable signal that the cancel was acknowledged.
        if track is not None and track.is_canceled:
            return (
                "cancelled", filled,
                status.get("avg_fill_price"),
                status.get("commission") or Decimal("0"),
                ib_status,
            )

    status = await ctx.ib.get_order_status(ib_order_id)
    ib_status = status.get("status") or ""
    logger.warning(
        '{"event": "CANCEL_SETTLE_TIMEOUT", "ib_order_id": "%s", "timeout_s": %.1f, '
        '"ib_status": "%s", "qty_filled": "%s"}',
        ib_order_id, timeout, ib_status, status.get("qty_filled") or Decimal("0"),
    )
    return (
        "timeout",
        status.get("qty_filled") or Decimal("0"),
        status.get("avg_fill_price"),
        status.get("commission") or Decimal("0"),
        ib_status,
    )


async def _handle_partial(
    order_ctx: _OrderContext, trade_group: TradeGroup, qty_requested: Decimal,
    qty_filled: Decimal, avg_price: Decimal, commission: Decimal,
    cmd, con_id: int, ib_order_id: str, ctx: AppContext,
) -> None:
    """Attempt to cancel the remainder, wait for IB's decision, then either
    promote to FILLED (cancel lost the race) or finalize as PARTIAL."""
    remainder = qty_requested - qty_filled

    ctx.router.emit(
        f"\u27f3 Attempting to cancel {remainder} remaining of "
        f"#{trade_group.serial_number}\u2026",
        pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
        event="ORDER_CANCEL_ATTEMPT_DISPLAY",
    )

    _write_txn(ctx, TransactionAction.CANCEL_ATTEMPT, order_ctx.symbol, order_ctx.side,
               order_ctx.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number,
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)

    track = ctx.tracker.get(ib_order_id)
    settle_timeout = float(ctx.settings.get("cancel_settle_timeout_seconds", 120))
    resolution, ib_reported_qty, ib_reported_avg, ib_reported_commission, _ib_status = \
        await _cancel_and_await_resolution(
            ctx, ib_order_id, qty_requested, track=track, timeout=settle_timeout,
            heartbeat_label=f"Cancel pending #{trade_group.serial_number} \u2014",
        )
    # The caller already had partial-fill data from the execDetails callback;
    # IB's orderStatus.filled can LAG those callbacks, so don't regress to a
    # smaller number. Take whichever source reports more fills.
    if ib_reported_qty > qty_filled:
        final_qty = ib_reported_qty
        effective_avg = ib_reported_avg if ib_reported_avg is not None else avg_price
    else:
        final_qty = qty_filled
        effective_avg = avg_price
    if ib_reported_commission > commission:
        commission = ib_reported_commission

    if resolution == "filled":
        # The cancel lost the race — IB filled the remainder. Record it as
        # a full fill so the trade group / positions / PnL reflect reality.
        logger.warning(
            '{"event": "CANCEL_FILL_RACE_RESOLVED", "ib_order_id": "%s", '
            '"serial": %d, "qty_filled": "%s", "resolution": "filled"}',
            ib_order_id, trade_group.serial_number, str(final_qty),
        )
        await _handle_fill(
            order_ctx, trade_group, final_qty, effective_avg, commission,
            cmd, con_id, ctx,
        )
        return

    # PARTIAL outcome. Use the final qty_filled (may be > the value we were
    # called with if more fills arrived while we were waiting on the cancel).
    _write_txn(ctx, TransactionAction.PARTIAL_FILL, order_ctx.symbol, order_ctx.side,
               order_ctx.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               ib_filled_qty=final_qty, ib_avg_fill_price=effective_avg,
               trade_serial=trade_group.serial_number,
               ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type,
               commission=commission)
    # pnl=None — partials on the entry side shouldn't clobber realized_pnl.
    ctx.trades.update_pnl(trade_group.id, None, commission)
    _write_txn(ctx, TransactionAction.CANCELLED, order_ctx.symbol, order_ctx.side,
               order_ctx.order_type, qty_requested,
               ib_order_id=_safe_int(ib_order_id),
               trade_serial=trade_group.serial_number, is_terminal=True,
               ib_responded_at=_now_utc(),
               trade_id=order_ctx.trade_id, leg_type=order_ctx.leg_type,
               correlation_id=order_ctx.correlation_id, security_type=order_ctx.security_type)

    final_remainder = qty_requested - final_qty
    cancel_note = "cancel confirmed" if resolution == "cancelled" else "cancel ack timeout"
    ctx.router.emit(
        f"\u26a0 PARTIAL: {final_qty}/{qty_requested} filled @ avg ${effective_avg} | "
        f"{final_remainder} shares not filled ({cancel_note})\n"
        f"  Commission: ${commission}\n"
        f"  Serial: #{trade_group.serial_number}",
        pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING,
        event="ORDER_PARTIAL_DISPLAY",
    )
    logger.info(
        '{"event": "ORDER_PARTIAL_FILL", "correlation_id": "%s", "serial": %d, '
        '"qty_filled": "%s", "qty_requested": "%s", "cancel_resolution": "%s"}',
        order_ctx.correlation_id, trade_group.serial_number,
        final_qty, qty_requested, resolution,
    )

    if cmd.take_profit_price or cmd.profit_amount:
        from ib_trader.repl.commands import BuyCommand as BC
        entry_side = "BUY" if isinstance(cmd, BC) else "SELL"
        await place_profit_taker(
            trade_id=trade_group.id,
            entry_side=entry_side,
            avg_fill_price=effective_avg,
            qty_filled=final_qty,
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
    target_qty: Decimal,
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
    amend_count: int = 0  # incremented per actual amendment (not per loop step,
                          # which includes dedup'd no-ops at tight spreads).

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

    paused_on_fill_at_step: int | None = None

    for step in range(1, total_steps + 1):
        track = ctx.tracker.get(ib_order_id)
        if track and track.is_filled:
            paused_on_fill_at_step = step
            break
        if track and track.is_canceled:
            break

        await asyncio.sleep(interval_seconds)

        track = ctx.tracker.get(ib_order_id)
        if track and track.is_filled:
            paused_on_fill_at_step = step
            break
        if track and track.is_canceled:
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
            # Emit a tick line so the user sees the walker is alive even
            # when the rounded step price matches what's already resting.
            _tick_status = await ctx.ib.get_order_status(ib_order_id)
            _tick_qf = _tick_status.get("qty_filled") or Decimal("0")
            ctx.router.emit(
                f"[{_now_display()}] Reprice tick {step}/{total_steps} \u2014 "
                f"holding at ${new_price} "
                f"(filled: {_fmt_qty(_tick_qf)}/{_fmt_qty(target_qty)})",
                pane=OutputPane.LOG, severity=OutputSeverity.INFO,
                event="REPRICE_TICK_UNCHANGED_DISPLAY",
            )
            continue

        await ctx.ib.amend_order(ib_order_id, new_price)
        last_sent_price = new_price
        amend_count += 1

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
            f"amend {amend_count} "
            f"(filled: {_fmt_qty(qty_filled)}/{_fmt_qty(target_qty)})",
            pane=OutputPane.LOG, severity=OutputSeverity.INFO,
            event="REPRICE_STEP_DISPLAY",
        )
        logger.info(
            '{"event": "REPRICE_STEP", "correlation_id": "%s", "step": %d, "total": %d, '
            '"amend": %d, "bid": "%s", "ask": "%s", "new_price": "%s"}',
            correlation_id, step, total_steps, amend_count, bid, ask, new_price,
        )

    # Walker broke on a partial fill (deliberate price-preservation choice).
    # Emit a line so the user sees why the reprice stream stopped; otherwise
    # the command feed goes silent between the partial fill and the passive
    # phase's "Walker complete" message.
    if paused_on_fill_at_step is not None:
        _paused_status = await ctx.ib.get_order_status(ib_order_id)
        _paused_qf = _paused_status.get("qty_filled") or Decimal("0")
        ctx.router.emit(
            f"[{_now_display()}] Walker paused at step "
            f"{paused_on_fill_at_step}/{total_steps} \u2014 order has fills, "
            f"holding at ${last_sent_price} "
            f"(filled: {_fmt_qty(_paused_qf)}/{_fmt_qty(target_qty)})",
            pane=OutputPane.LOG, severity=OutputSeverity.INFO,
            event="REPRICE_PAUSED_DISPLAY",
        )

    logger.info(
        '{"event": "REPRICE_TIMEOUT", "correlation_id": "%s", "amends": %d, '
        '"paused_on_fill_at_step": %s}',
        correlation_id, amend_count,
        paused_on_fill_at_step if paused_on_fill_at_step is not None else 'null',
    )


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
        if is_outside_rth():
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
            ctx.router.emit(
                f"[{_now_display()}] Filled {_fmt_qty(q)} @ ${avg} "
                f"({_fmt_qty(_fill_qty)}/{_fmt_qty(qty_to_close)})",
                pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                event="ORDER_PARTIAL_FILL_DISPLAY",
            )

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
        total_steps = int(settings.get("reprice_steps", 10))
        reprice_task = asyncio.create_task(
            reprice_loop(
                correlation_id=close_ctx.correlation_id,
                ib_order_id=ib_order_id,
                con_id=con_id,
                symbol=entry_symbol,
                side=close_side,
                ctx=ctx,
                total_steps=total_steps,
                interval_seconds=_reprice_interval(settings),
                initial_price=initial_price,
                target_qty=qty_to_close,
                trade_id=close_ctx.trade_id,
                leg_type=close_ctx.leg_type,
                security_type=close_ctx.security_type,
                trade_serial=cmd.serial,
            )
        )
        wait_timeout = float(settings["reprice_active_duration_seconds"]) + 2
    else:
        # BID / ASK / MARKET — single unified give-up window.
        wait_timeout = _total_order_wait(settings)

    # ── Wait for fill or timeout ─────────────────────────────────────────
    await _await_full_fill_or_timeout(
        track, ib_order_id, qty_to_close, wait_timeout, ctx,
    )

    if reprice_task:
        reprice_task.cancel()
        try:
            await reprice_task
        except asyncio.CancelledError:
            pass

        # Passive phase (MID only): hold the last-amended limit and wait
        # for IB to deliver residual fills. Mirrors `_execute_mid_order`.
        _close_status = await ctx.ib.get_order_status(ib_order_id)
        _close_filled = _close_status.get("qty_filled") or Decimal("0")
        if _close_filled < qty_to_close and not track.is_canceled:
            passive_wait = float(settings["reprice_passive_wait_seconds"])
            if passive_wait > 0:
                ctx.router.emit(
                    f"[{_now_display()}] Walker complete \u2014 holding at "
                    f"last amended price, waiting up to {int(passive_wait)}s "
                    f"for residual\u2026",
                    pane=OutputPane.COMMAND, severity=OutputSeverity.INFO,
                    event="ORDER_PASSIVE_WAIT_DISPLAY",
                )
                await _await_full_fill_or_timeout(
                    track, ib_order_id, qty_to_close, passive_wait, ctx,
                )

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
