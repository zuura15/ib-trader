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
from ib_trader.data.models import (
    LegType, Order, OrderStatus, RepriceEvent, SecurityType, TradeGroup, TradeStatus
)
from ib_trader.engine.exceptions import IBOrderRejectedError, SafetyLimitError, TradeNotFoundError
from ib_trader.engine.pricing import (
    calc_mid, calc_profit_taker_price, calc_profit_taker_price_short, calc_step_price,
    calc_shares_from_dollars,
)
from ib_trader.repl.commands import BuyCommand, SellCommand, CloseCommand, Strategy

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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
            print("\u2717 Error: dollar amount too small for current price")
            return

    if qty is None or qty <= 0:
        print("\u2717 Error: quantity must be a positive number")
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
        if cmd.strategy == Strategy.MID:
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

    print(
        f"Order #{trade_group.serial_number} \u2014 {side} {qty} {cmd.symbol} @ mid\n"
        f"[{_now_utc().strftime('%H:%M:%S')}] Placed @ ${mid} "
        f"(bid: ${bid} ask: ${ask})"
    )

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, mid, outside_rth=True, tif="GTC"
    )

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

    ctx.ib.register_fill_callback(on_fill)
    ctx.ib.register_status_callback(on_status)

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
        logger.error(
            '{"event": "ORDER_PLACEMENT_FAILED", "order_id": "%s", '
            '"serial": %d, "ib_order_id": "%s", "ib_status": "%s", "reason": "%s"}',
            order.id, trade_group.serial_number, ib_order_id, _ib_status, reason,
        )
        raise IBOrderRejectedError(reason)

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
            await _handle_partial(order, trade_group, qty, qty_filled, avg_price, commission, cmd, con_id, ctx)
    else:
        # No shares filled — cancel any remaining open order and report expired.
        await ctx.ib.cancel_order(ib_order_id)
        ctx.orders.update_status(order.id, OrderStatus.CANCELED)
        ctx.trades.update_status(trade_group.id, TradeStatus.CLOSED)
        print(
            f"\u2717 EXPIRED: 0/{qty} filled | reprice window closed "
            f"({settings['reprice_duration_seconds']}s)\n"
            f"  Serial: #{trade_group.serial_number}"
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

    print(
        f"Order #{trade_group.serial_number} \u2014 {side} {qty} {cmd.symbol} @ {cmd.strategy}\n"
        f"[{_now_utc().strftime('%H:%M:%S')}] Placed @ ${price} "
        f"(bid: ${bid} ask: ${ask})"
    )

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, cmd.symbol, side, qty, price, outside_rth=True, tif="GTC"
    )

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

    ctx.ib.register_fill_callback(on_fill)
    ctx.ib.register_status_callback(on_status)

    # Wait briefly to catch immediate fills (e.g. ask buy fills at once).
    # If not filled within 30 s, leave the GTC order live — daemon reconciler
    # will update the DB when IB eventually reports the fill.
    try:
        await asyncio.wait_for(track.fill_event.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        pass

    status = await ctx.ib.get_order_status(ib_order_id)
    qty_filled = status["qty_filled"]
    avg_price = status["avg_fill_price"]
    commission = _fill_commission if _fill_commission is not None else (status["commission"] or Decimal("0"))

    if qty_filled > 0 and avg_price is not None:
        if qty_filled >= qty:
            await _handle_fill(order, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
        else:
            await _handle_partial(order, trade_group, qty, qty_filled, avg_price, commission, cmd, con_id, ctx)
    else:
        # Order live in IB as GTC — leave trade/order OPEN, daemon will reconcile the fill.
        print(
            f"\u25cf LIVE: {qty} {cmd.symbol} @ ${price} ({cmd.strategy}) — "
            f"GTC order active in IB\n"
            f"  Serial: #{trade_group.serial_number}"
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
    """Place a market order and wait for fill."""
    ib_order_id = await ctx.ib.place_market_order(con_id, cmd.symbol, side, qty, outside_rth=True)

    ctx.orders.update_ib_order_id(order.id, ib_order_id)
    ctx.orders.update_status(order.id, OrderStatus.OPEN)

    track = ctx.tracker.register(order.id, ib_order_id, cmd.symbol)

    async def on_fill(fill_ib_id: str, qty_filled: Decimal, avg_price: Decimal, commission: Decimal):
        if fill_ib_id == ib_order_id:
            ctx.tracker.notify_filled(fill_ib_id)

    ctx.ib.register_fill_callback(on_fill)

    # Market orders should fill quickly — wait up to 30 seconds
    try:
        await asyncio.wait_for(track.fill_event.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        pass

    status = await ctx.ib.get_order_status(ib_order_id)
    qty_filled = status["qty_filled"]
    avg_price = status["avg_fill_price"]
    commission = status["commission"] or Decimal("0")

    if qty_filled > 0:
        await _handle_fill(order, trade_group, qty_filled, avg_price, commission, cmd, con_id, ctx)
    else:
        ctx.orders.update_status(order.id, OrderStatus.CANCELED)
        print(f"\u2717 CANCELED: market order did not fill\n  Serial: #{trade_group.serial_number}")

    ctx.tracker.unregister(ib_order_id)


async def _handle_fill(
    order: Order, trade_group: TradeGroup, qty_filled: Decimal,
    avg_price: Decimal, commission: Decimal, cmd, con_id: int, ctx: AppContext,
) -> None:
    """Record a complete fill and place profit taker if configured."""
    ctx.orders.update_fill(order.id, qty_filled, avg_price, commission)
    ctx.orders.update_status(order.id, OrderStatus.FILLED)
    ctx.trades.update_pnl(trade_group.id, Decimal("0"), commission)

    print(
        f"\u2713 FILLED: {qty_filled} shares {order.symbol} @ ${avg_price} avg\n"
        f"  Commission: ${commission}\n"
        f"  Serial: #{trade_group.serial_number}"
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
    cmd, con_id: int, ctx: AppContext,
) -> None:
    """Record a partial fill."""
    ctx.orders.update_fill(order.id, qty_filled, avg_price, commission)
    ctx.orders.update_status(order.id, OrderStatus.PARTIAL)
    ctx.trades.update_pnl(trade_group.id, Decimal("0"), commission)

    remainder = qty_requested - qty_filled
    print(
        f"\u26a0 PARTIAL: {qty_filled}/{qty_requested} filled @ avg ${avg_price} | "
        f"{remainder} shares canceled (timeout)\n"
        f"  Commission: ${commission}\n"
        f"  Serial: #{trade_group.serial_number}"
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

        print(
            f"[{now.strftime('%H:%M:%S')}] Amended \u2192 ${new_price} | "
            f"step {step}/{total_steps} "
            f"(still open: {qty_filled}/? filled)"
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

    ib_order_id = await ctx.ib.place_limit_order(
        con_id, symbol, pt_side, qty_filled, pt_price,
        outside_rth=True, tif="GTC",
    )

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

    print(
        f"  Profit taker placed @ ${pt_price} (linked to #{ctx.trades.get_by_serial})"
    )
    logger.info(
        '{"event": "PROFIT_TAKER_PLACED", "trade_id": "%s", "symbol": "%s", '
        '"side": "%s", "price": "%s", "ib_order_id": "%s"}',
        trade_id, symbol, pt_side, pt_price, ib_order_id,
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
        print(f"\u2717 Error: no trade found with serial #{cmd.serial}")
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
        print(f"\u2717 Error: order #{cmd.serial} has no filled quantity to close")
        return

    qty_to_close = entry_order.qty_filled
    if qty_to_close == 0:
        print(f"\u2717 Error: order #{cmd.serial} has no filled quantity to close")
        return

    # Cancel all linked open orders (profit taker, stop loss)
    open_legs = ctx.orders.get_open_for_trade(trade_group.id)
    for leg in open_legs:
        if leg.leg_type in (LegType.PROFIT_TAKER, LegType.STOP_LOSS) and leg.ib_order_id:
            await ctx.ib.cancel_order(leg.ib_order_id)
            ctx.orders.update_status(leg.id, OrderStatus.CANCELED)
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

    # Execute the closing order using same logic as execute_order
    # Build a synthetic BuyCommand/SellCommand for the closing leg
    if cmd.strategy == Strategy.MID:
        snapshot = await ctx.ib.get_market_snapshot(con_id)
        bid, ask, last = snapshot["bid"], snapshot["ask"], snapshot["last"]

        if bid == 0 and ask == 0:
            if last == 0:
                raise ValueError(
                    f"Cannot close {entry_order.symbol}: no market data available "
                    "(bid=0, ask=0, last=0). Check market data subscription."
                )
            logger.warning(
                '{"event": "NO_BID_ASK_USING_LAST", "symbol": "%s", "last": "%s", '
                '"reason": "bid/ask unavailable, market likely closed"}',
                entry_order.symbol, last,
            )
            bid = ask = last

        mid = calc_mid(bid, ask)

        ib_order_id = await ctx.ib.place_limit_order(
            con_id, entry_order.symbol, close_side, qty_to_close, mid,
            outside_rth=True, tif="GTC",
        )
        ctx.orders.update_ib_order_id(close_order.id, ib_order_id)
        ctx.orders.update_status(close_order.id, OrderStatus.OPEN)

        print(
            f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Close #{cmd.serial} placed "
            f"@ ${mid} (bid: ${bid} ask: ${ask})"
        )
    else:
        ib_order_id = await ctx.ib.place_market_order(
            con_id, entry_order.symbol, close_side, qty_to_close, outside_rth=True
        )
        ctx.orders.update_ib_order_id(close_order.id, ib_order_id)
        ctx.orders.update_status(close_order.id, OrderStatus.OPEN)
        print(f"Close #{cmd.serial} market order placed.")
