"""Middleware pipeline for the bot runtime.

Actions returned by strategies pass through middleware in order:
  RiskMiddleware → LoggingMiddleware → PersistenceMiddleware → ExecutionMiddleware

Each middleware can inspect, modify, or reject actions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_trader.bots.state import BotStateStore


from ib_trader.bots.strategy import (
    Action, PlaceOrder, CancelOrder, UpdateState, LogSignal,
    StrategyContext, LogEventType,
)
from ib_trader.data.models import BotEvent
from ib_trader.data.repositories.bot_repository import BotEventRepository
# TODO(redis-positions): RiskMiddleware.max_positions reads open trades
# from SQLite. Migrate to Redis position state or IB positions so this
# import can be dropped from the bot hot-path.
from ib_trader.data.repository import TradeRepository

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Manual-Entry Middleware
# ---------------------------------------------------------------------------

class ManualEntryMiddleware:
    """Blocks auto-BUY entries from a bot's strategy signals.

    When a bot's YAML has ``manual_entry_only: true`` this middleware
    filters out ``PlaceOrder(side="BUY", origin="strategy")`` actions —
    i.e. the normal "my signal says buy" path. Every other action goes
    through untouched, so the rest of production behaviour is preserved:

      - ``origin="exit"``        — trailing stops, hard stops, time stops,
                                   strategy-emitted sell-to-close.
      - ``origin="manual_override"`` — operator FORCE_BUY signal (which
                                   is routed outside ``on_event`` anyway).
      - ``side="SELL"``          — never blocked here.

    The point is to let a live test bot run the FULL production strategy
    (exits, trailing stop, P&L display, middleware pipeline) while
    keeping auto-entries off the wire, so the only real BUYs come from
    a deliberate operator action.

    When ``manual_entry_only`` is False (the default), the middleware is
    a no-op pass-through.
    """

    def __init__(self, bot_id: str, manual_entry_only: bool = False) -> None:
        self.bot_id = bot_id
        self.enabled = bool(manual_entry_only)

    def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        if not self.enabled:
            return actions
        result: list[Action] = []
        for action in actions:
            if (
                isinstance(action, PlaceOrder)
                and action.side == "BUY"
                and getattr(action, "origin", "strategy") == "strategy"
            ):
                # Drop the auto-entry, leave an audit breadcrumb so it's
                # obvious from bot_events that the bot wanted to buy but
                # the manual-entry gate stopped it.
                result.append(LogSignal(
                    event_type=LogEventType.MANUAL_ENTRY_ONLY,
                    message=(
                        f"Blocked strategy BUY for {action.symbol} "
                        f"qty={action.qty} — manual_entry_only=true"
                    ),
                    payload={
                        "symbol": action.symbol,
                        "side": action.side,
                        "qty": str(action.qty),
                        "origin": getattr(action, "origin", "strategy"),
                    },
                ))
                continue
            result.append(action)
        return result


# ---------------------------------------------------------------------------
# Risk Middleware
# ---------------------------------------------------------------------------

class RiskMiddleware:
    """Enforces risk controls on PlaceOrder actions.

    Checks: kill switch, daily loss cap, max concurrent positions,
    max trades per day, max position value, max shares.

    KILL_SWITCH and daily counters are sourced from Redis via
    ``BotStateStore`` so they survive runner restarts. The previous
    in-process counters reset to 0 on every restart, which silently
    disabled the daily loss cap and the trades-per-day cap after a
    crash. The kill-switch read is fail-closed.
    """

    def __init__(self, bot_id: str, risk_config: dict,
                 trades_repo: TradeRepository,
                 state_store: BotStateStore) -> None:
        self.bot_id = bot_id
        self.config = risk_config
        self._trades = trades_repo
        self._state = state_store

    async def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Filter actions through risk checks. Rejected PlaceOrders become LogSignals.

        SELL orders are NEVER blocked — exit protection is more important
        than risk limits. Only BUY orders go through risk checks.
        """
        result: list[Action] = []
        for action in actions:
            if isinstance(action, PlaceOrder) and action.side == "BUY":
                ok, reason = await self._can_trade(action)
                if not ok:
                    result.append(LogSignal(
                        event_type=LogEventType.RISK,
                        message=f"Order blocked: {reason}",
                        payload={"symbol": action.symbol, "side": action.side,
                                 "qty": str(action.qty), "reason": reason},
                    ))
                    continue
            result.append(action)
        return result

    async def record_trade(self) -> None:
        """Increment daily + lifetime trade counters in Redis."""
        await self._state.record_trade(self.bot_id)

    async def record_pnl(self, pnl: Decimal) -> None:
        """Add realized P&L to today's running total in Redis."""
        await self._state.record_pnl(self.bot_id, pnl)

    async def _can_trade(self, order: PlaceOrder) -> tuple[bool, str]:
        """Check all risk rules. Returns (ok, reason)."""
        if await self._state.is_kill_switch_engaged(self.bot_id):
            return False, "kill_switch_active"

        stats = await self._state.get_stats(self.bot_id)
        trades_today = int(stats.get("trades_today") or 0)
        pnl_today = Decimal(str(stats.get("pnl_today") or "0"))

        # Daily loss cap
        max_loss = Decimal(str(self.config.get("max_daily_loss_pct", 0.02)))
        account_value = Decimal(str(self.config.get("account_value", "10000")))
        if pnl_today < -(max_loss * account_value):
            return False, f"daily_loss_cap ({pnl_today})"

        # Max trades per day
        max_trades = self.config.get("max_trades_per_day", 10)
        if trades_today >= max_trades:
            return False, f"max_trades_per_day ({trades_today}/{max_trades})"

        # Max concurrent positions
        max_positions = self.config.get("max_concurrent_positions", 1)
        if order.side == "BUY":
            open_trades = self._trades.get_open()
            bot_source = f"bot:{self.bot_id}"
            bot_trades = [t for t in open_trades
                          if hasattr(t, "source") and t.source == bot_source]
            if len(bot_trades) >= max_positions:
                return False, f"max_positions ({len(bot_trades)}/{max_positions})"

        # Max position value
        max_value = Decimal(str(self.config.get("max_position_value", "10000")))
        if order.price and order.qty * order.price > max_value:
            return False, f"max_position_value ({order.qty * order.price} > {max_value})"

        # Max shares
        max_shares = self.config.get("max_shares", 20)
        if order.qty > max_shares:
            return False, f"max_shares ({order.qty} > {max_shares})"

        return True, ""


# ---------------------------------------------------------------------------
# Logging Middleware
# ---------------------------------------------------------------------------

class LoggingMiddleware:
    """Writes LogSignal actions to the bot_events audit trail."""

    def __init__(self, bot_id: str, bot_events_repo: BotEventRepository,
                 redis=None) -> None:
        self.bot_id = bot_id
        self._events = bot_events_repo
        self._redis = redis

    async def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Write LogSignal actions to DB. Pass all actions through."""
        wrote = False
        for action in actions:
            if isinstance(action, LogSignal):
                self._events.insert(BotEvent(
                    bot_id=self.bot_id,
                    event_type=action.event_type,
                    message=action.message,
                    payload_json=json.dumps(action.payload, default=str) if action.payload else None,
                    trade_serial=action.trade_serial,
                    recorded_at=_now_utc(),
                ))
                wrote = True
        if wrote and self._redis is not None:
            from ib_trader.redis.streams import publish_activity
            await publish_activity(self._redis, "bot_events")
        return actions


# ---------------------------------------------------------------------------
# Persistence Middleware
# ---------------------------------------------------------------------------

class PersistenceMiddleware:
    """Persists UpdateState actions via the runtime's single write path.

    Receives a ``write_fn`` callback from the runtime — the ONLY way
    to write to the bot's Redis state key.  This middleware does not
    hold a Redis handle for state writes, so direct ``store.set`` calls
    are structurally impossible.
    """

    def __init__(self, bot_id: str, write_fn) -> None:
        self.bot_id = bot_id
        self._write = write_fn

    async def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Apply UpdateState actions via the write callback.

        Async: awaits the Redis write to ensure state is persisted BEFORE
        the next middleware (ExecutionMiddleware) places any orders.
        """
        for action in actions:
            if isinstance(action, UpdateState):
                await self._write(action.state)
        return actions


# ---------------------------------------------------------------------------
# Execution Middleware
# ---------------------------------------------------------------------------

class ExecutionMiddleware:
    """Submits PlaceOrder and CancelOrder actions to the engine via HTTP."""

    def __init__(self, bot_id: str,
                 engine_url: str | None = None,
                 bot_ref: str | None = None) -> None:
        self.bot_id = bot_id
        self._engine_url = engine_url  # e.g., "http://127.0.0.1:8081"
        self._bot_ref = bot_ref
        self.last_cmd_id: str | None = None
        self.last_order_ref: str | None = None

    async def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Convert PlaceOrder/CancelOrder to engine HTTP orders (async)."""
        if not self._engine_url:
            raise RuntimeError("Engine URL not configured — cannot place orders")

        for action in actions:
            if isinstance(action, PlaceOrder):
                await self._submit_via_http(action, ctx)
            elif isinstance(action, CancelOrder):
                await self._cancel_via_http(action)

        return actions

    async def _submit_via_http(self, order: PlaceOrder, ctx: StrategyContext) -> None:
        """Submit order via async HTTP POST to engine internal API.

        All orders (BUY and SELL) go through /engine/orders. The bot
        has symbol, qty, and order_type — it doesn't need to route
        through /engine/close (which requires a SQLite trade serial).
        """
        import httpx

        payload = {
            "symbol": order.symbol,
            "side": order.side,
            "qty": str(order.qty),
            "order_type": order.order_type,
            "bot_ref": self._bot_ref,
        }
        if order.price is not None:
            payload["price"] = str(order.price)
        if order.params.get("profit_target") is not None:
            payload["profit"] = str(order.params["profit_target"])
        if order.params.get("stop_loss") is not None:
            payload["stop_loss"] = str(order.params["stop_loss"])

        url = f"{self._engine_url}/engine/orders"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            result = resp.json()
            self.last_cmd_id = result.get("ib_order_id")
            self.last_order_ref = result.get("order_ref")
            logger.info(
                '{"event": "BOT_ORDER_HTTP", "bot_id": "%s", "symbol": "%s", '
                '"side": "%s", "order_ref": "%s"}',
                self.bot_id, order.symbol, order.side, self.last_order_ref,
            )

    async def _cancel_via_http(self, action: CancelOrder) -> None:
        """Cancel order via async HTTP POST to engine."""
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._engine_url}/engine/close",
                json={"serial": action.trade_serial, "strategy": "market"},
            )
            resp.raise_for_status()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class MiddlewarePipeline:
    """Runs actions through middleware in order."""

    def __init__(self, middlewares: list, rollback_fn=None) -> None:
        self._middlewares = middlewares
        self._rollback_fn = rollback_fn
        self.last_cmd_id: str | None = None

    async def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Pass actions through each middleware sequentially.

        Supports both sync and async middleware process() methods.
        If ExecutionMiddleware fails, rolls back any state changes made
        by PersistenceMiddleware to prevent stuck ENTERING/EXITING states.
        """
        import asyncio
        state_snapshot = dict(ctx.state)  # Snapshot before pipeline runs

        for mw in self._middlewares:
            try:
                result = mw.process(actions, ctx)
                if asyncio.iscoroutine(result):
                    actions = await result
                else:
                    actions = result
            except Exception:
                # If execution failed, rollback state to pre-pipeline snapshot
                if isinstance(mw, ExecutionMiddleware) and self._rollback_fn:
                    try:
                        await self._rollback_fn(state_snapshot)
                    except Exception as rollback_err:
                        logger.debug("PIPELINE_ROLLBACK_FAILED", exc_info=rollback_err)
                    logger.warning(
                        '{"event": "PIPELINE_STATE_ROLLBACK", "reason": "execution_failed"}'
                    )
                raise

            # Capture command ID if execution middleware submitted an order
            if isinstance(mw, ExecutionMiddleware) and mw.last_cmd_id:
                self.last_cmd_id = mw.last_cmd_id
                mw.last_cmd_id = None  # consume it
        return actions
