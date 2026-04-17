"""Middleware pipeline for the bot runtime.

Actions returned by strategies pass through middleware in order:
  RiskMiddleware → LoggingMiddleware → PersistenceMiddleware → ExecutionMiddleware

Each middleware can inspect, modify, or reject actions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_trader.bots.state import BotStateStore

from sqlalchemy.orm import scoped_session

from ib_trader.bots.strategy import (
    Action, PlaceOrder, CancelOrder, UpdateState, LogSignal,
    StrategyContext, PositionState,
)
from ib_trader.data.models import PendingCommand, PendingCommandStatus, BotEvent
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
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
                    event_type="MANUAL_ENTRY_ONLY",
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

    KILL_SWITCH source of truth is Redis via ``BotStateStore`` (hot-path
    µs read). The check is fail-closed: if Redis is unreachable or the
    read raises, the middleware treats the switch as engaged and
    rejects BUYs. An optional SQLite fallback (legacy ``BotRepository``)
    is still accepted for backwards compatibility during the migration
    — once ``state_store`` is present, SQLite is not consulted.
    """

    def __init__(self, bot_id: str, risk_config: dict,
                 bots_repo: BotRepository, trades_repo: TradeRepository,
                 state_store: "BotStateStore | None" = None) -> None:
        self.bot_id = bot_id
        self.config = risk_config
        self._bots = bots_repo
        self._trades = trades_repo
        self._state = state_store
        self._trades_today: int = 0
        self._pnl_today: Decimal = Decimal("0")
        self._last_reset_date: date = date.today()

    async def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Filter actions through risk checks. Rejected PlaceOrders become LogSignals.

        SELL orders are NEVER blocked — exit protection is more important
        than risk limits. Only BUY orders go through risk checks.
        """
        self._maybe_reset_daily()
        result: list[Action] = []
        for action in actions:
            if isinstance(action, PlaceOrder) and action.side == "BUY":
                ok, reason = await self._can_trade(action)
                if not ok:
                    result.append(LogSignal(
                        event_type="RISK",
                        message=f"Order blocked: {reason}",
                        payload={"symbol": action.symbol, "side": action.side,
                                 "qty": str(action.qty), "reason": reason},
                    ))
                    continue
            result.append(action)
        return result

    def record_trade(self) -> None:
        """Increment daily trade counter after a fill."""
        self._trades_today += 1

    def record_pnl(self, pnl: Decimal) -> None:
        """Update daily P&L after a close."""
        self._pnl_today += pnl

    async def _can_trade(self, order: PlaceOrder) -> tuple[bool, str]:
        """Check all risk rules. Returns (ok, reason)."""
        # Kill switch — prefer Redis (hot-path, fail-closed). Fall back to
        # SQLite only if no state_store was provided (legacy callers).
        if self._state is not None:
            engaged = await self._state.is_kill_switch_engaged(self.bot_id)
            if engaged:
                return False, "kill_switch_active"
        else:
            bot = self._bots.get(self.bot_id) if self._bots else None
            if bot and getattr(bot, "error_message", None) == "KILL_SWITCH":
                return False, "kill_switch_active"

        # Daily loss cap
        max_loss = Decimal(str(self.config.get("max_daily_loss_pct", 0.02)))
        account_value = Decimal(str(self.config.get("account_value", "10000")))
        if self._pnl_today < -(max_loss * account_value):
            return False, f"daily_loss_cap ({self._pnl_today})"

        # Max trades per day
        max_trades = self.config.get("max_trades_per_day", 10)
        if self._trades_today >= max_trades:
            return False, f"max_trades_per_day ({self._trades_today}/{max_trades})"

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

    def _maybe_reset_daily(self) -> None:
        """Reset daily counters at midnight."""
        today = date.today()
        if today != self._last_reset_date:
            self._trades_today = 0
            self._pnl_today = Decimal("0")
            self._last_reset_date = today


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
    """Persists UpdateState actions to Redis key (primary) and JSON file (fallback)."""

    def __init__(self, bot_id: str, symbol: str, state_dir: Path,
                 redis=None, bot_ref: str | None = None) -> None:
        self.bot_id = bot_id
        self.symbol = symbol
        self.state_dir = state_dir
        self._redis = redis
        self._bot_ref = bot_ref or bot_id

    async def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Apply UpdateState actions to ctx.state and flush to Redis.

        Async: awaits the Redis write to ensure state is persisted BEFORE
        the next middleware (ExecutionMiddleware) places any orders.
        """
        for action in actions:
            if isinstance(action, UpdateState):
                ctx.state.update(action.state)
                await self._flush(ctx.state)
        return actions

    async def _flush(self, state: dict) -> None:
        """Write state to Redis key, then XADD a marker to bot:state stream.

        The stream marker is the "something changed" signal that the
        WebSocket consumer uses to push the latest snapshot to the UI.
        """
        if self._redis is None:
            raise RuntimeError(
                f"Redis not available — cannot persist strategy state for {self._bot_ref}:{self.symbol}"
            )

        from ib_trader.redis.state import StateStore
        from ib_trader.redis.streams import publish_activity

        store = StateStore(self._redis)
        # Write to the single consolidated bot key: bot:<uuid>
        # Merge with existing doc to preserve FSM fields
        key = f"bot:{self.bot_id}"
        existing = await store.get(key) or {}
        merged = {**existing, **state}
        await store.set(key, merged)

        # Nudge WS bots channel
        try:
            await publish_activity(self._redis, "bots")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Execution Middleware
# ---------------------------------------------------------------------------

class ExecutionMiddleware:
    """Submits PlaceOrder and CancelOrder actions to the engine.

    Primary path: HTTP POST to engine's internal API (synchronous).
    Fallback path: pending_commands SQLite table (polling-based).
    """

    def __init__(self, bot_id: str,
                 pending_commands_repo: PendingCommandRepository,
                 engine_url: str | None = None,
                 bot_ref: str | None = None) -> None:
        self.bot_id = bot_id
        self._commands = pending_commands_repo
        self._engine_url = engine_url  # e.g., "http://127.0.0.1:8081"
        self._bot_ref = bot_ref
        self.last_cmd_id: str | None = None
        self.last_order_ref: str | None = None
        self.last_serial: int | None = None

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
        """Submit order via async HTTP POST to engine internal API."""
        import httpx

        serial = ctx.state.get("trade_serial", 0)
        payload = {
            "symbol": order.symbol,
            "side": order.side,
            "qty": str(order.qty),
            "order_type": order.order_type,
            "bot_ref": self._bot_ref,
            "serial": serial,
        }
        if order.price is not None:
            payload["price"] = str(order.price)
        if order.params.get("profit_target") is not None:
            payload["profit"] = str(order.params["profit_target"])
        if order.params.get("stop_loss") is not None:
            payload["stop_loss"] = str(order.params["stop_loss"])

        # Use close endpoint for SELL orders with a serial
        if order.side == "SELL" and serial:
            url = f"{self._engine_url}/engine/close"
            payload = {
                "serial": serial,
                "strategy": order.order_type,
                "bot_ref": self._bot_ref,
            }
        else:
            url = f"{self._engine_url}/engine/orders"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            result = resp.json()
            self.last_cmd_id = result.get("ib_order_id")
            self.last_order_ref = result.get("order_ref")
            self.last_serial = result.get("serial", serial)
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

    def __init__(self, middlewares: list) -> None:
        self._middlewares = middlewares
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
                if isinstance(mw, ExecutionMiddleware):
                    ctx.state.update(state_snapshot)
                    # Persist the rollback to Redis
                    for prev_mw in self._middlewares:
                        if isinstance(prev_mw, PersistenceMiddleware):
                            try:
                                await prev_mw._flush(ctx.state)
                            except Exception:
                                pass
                    logger.warning(
                        '{"event": "PIPELINE_STATE_ROLLBACK", "reason": "execution_failed"}'
                    )
                raise

            # Capture command ID if execution middleware submitted an order
            if isinstance(mw, ExecutionMiddleware) and mw.last_cmd_id:
                self.last_cmd_id = mw.last_cmd_id
                mw.last_cmd_id = None  # consume it
        return actions
