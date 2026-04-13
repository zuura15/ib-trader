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
# Risk Middleware
# ---------------------------------------------------------------------------

class RiskMiddleware:
    """Enforces risk controls on PlaceOrder actions.

    Checks: kill switch, daily loss cap, max concurrent positions,
    max trades per day, max position value, max shares.
    """

    def __init__(self, bot_id: str, risk_config: dict,
                 bots_repo: BotRepository, trades_repo: TradeRepository) -> None:
        self.bot_id = bot_id
        self.config = risk_config
        self._bots = bots_repo
        self._trades = trades_repo
        self._trades_today: int = 0
        self._pnl_today: Decimal = Decimal("0")
        self._last_reset_date: date = date.today()

    def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Filter actions through risk checks. Rejected PlaceOrders become LogSignals.

        SELL orders are NEVER blocked — exit protection is more important
        than risk limits. Only BUY orders go through risk checks.
        """
        self._maybe_reset_daily()
        result = []
        for action in actions:
            if isinstance(action, PlaceOrder) and action.side == "BUY":
                ok, reason = self._can_trade(action)
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

    def _can_trade(self, order: PlaceOrder) -> tuple[bool, str]:
        """Check all risk rules. Returns (ok, reason)."""
        # Kill switch
        bot = self._bots.get(self.bot_id)
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

    def __init__(self, bot_id: str, bot_events_repo: BotEventRepository) -> None:
        self.bot_id = bot_id
        self._events = bot_events_repo

    def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Write LogSignal actions to DB. Pass all actions through."""
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

    def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Apply UpdateState actions to ctx.state and flush to Redis + disk."""
        for action in actions:
            if isinstance(action, UpdateState):
                ctx.state.update(action.state)
                self._flush(ctx.state)
        return actions

    def _flush(self, state: dict) -> None:
        """Write state to Redis key. Fails loud if Redis is unavailable."""
        if self._redis is None:
            raise RuntimeError(
                f"Redis not available — cannot persist strategy state for {self._bot_ref}:{self.symbol}"
            )

        import asyncio
        from ib_trader.redis.state import StateStore, StateKeys
        store = StateStore(self._redis)
        key = StateKeys.strategy(self._bot_ref, self.symbol)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(store.set(key, state))
        else:
            loop.run_until_complete(store.set(key, state))


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

    def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Convert PlaceOrder/CancelOrder to engine HTTP orders."""
        if not self._engine_url:
            raise RuntimeError("Engine URL not configured — cannot place orders")

        for action in actions:
            if isinstance(action, PlaceOrder):
                self._submit_via_http(action, ctx)
            elif isinstance(action, CancelOrder):
                self._cancel_via_http(action)

        return actions

    def _submit_via_http(self, order: PlaceOrder, ctx: StrategyContext) -> None:
        """Submit order via HTTP POST to engine internal API."""
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
        if order.profit is not None:
            payload["profit"] = str(order.profit)

        # Use close endpoint for SELL orders with a serial
        if order.side == "SELL" and serial:
            url = f"{self._engine_url}/engine/close"
            payload = {
                "serial": serial,
                "strategy": order.order_type,
            }
        else:
            url = f"{self._engine_url}/engine/orders"

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(url, json=payload)
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
        except Exception:
            logger.exception(
                '{"event": "BOT_ORDER_HTTP_FAILED", "bot_id": "%s", "symbol": "%s"}',
                self.bot_id, order.symbol,
            )
            raise

    def _cancel_via_http(self, action: CancelOrder) -> None:
        """Cancel order via HTTP POST to engine."""
        import httpx
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{self._engine_url}/engine/close",
                    json={"serial": action.trade_serial, "strategy": "market"},
                )
                resp.raise_for_status()
        except Exception:
            logger.exception('{"event": "BOT_CANCEL_HTTP_FAILED"}')


        return actions

    def _build_order_command(self, order: PlaceOrder, ctx: StrategyContext) -> str:
        """Convert a PlaceOrder action to a command string.

        SELL orders use 'close SERIAL STRATEGY' format to properly close
        an existing position. BUY orders use 'buy SYMBOL QTY STRATEGY'.
        """
        if order.side == "SELL":
            # Use close command with the active trade serial
            serial = ctx.state.get("trade_serial")
            if serial:
                return f"close {serial} {order.order_type}"
            # Fallback: sell directly (shouldn't happen)
            return f"sell {order.symbol} {order.qty} {order.order_type}"

        # BUY order
        parts = ["buy", order.symbol, str(order.qty), order.order_type]

        if order.order_type == "limit" and order.price is not None:
            parts.append(str(order.price))

        for key, value in order.params.items():
            if key == "profit_target" and value is not None:
                parts.extend(["--profit", str(value)])
            elif key == "stop_loss" and value is not None:
                parts.extend(["--stop-loss", str(value)])

        return " ".join(parts)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class MiddlewarePipeline:
    """Runs actions through middleware in order."""

    def __init__(self, middlewares: list) -> None:
        self._middlewares = middlewares
        self.last_cmd_id: str | None = None

    def process(self, actions: list[Action], ctx: StrategyContext) -> list[Action]:
        """Pass actions through each middleware sequentially."""
        for mw in self._middlewares:
            actions = mw.process(actions, ctx)
            # Capture command ID if execution middleware submitted an order
            if isinstance(mw, ExecutionMiddleware) and mw.last_cmd_id:
                self.last_cmd_id = mw.last_cmd_id
                mw.last_cmd_id = None  # consume it
        return actions
