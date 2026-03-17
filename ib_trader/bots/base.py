"""Abstract bot base class.

Bots are strategies that run in the bot runner process. They submit
commands to the engine via the pending_commands table and read market
state from SQLite. They NEVER hold broker connections directly.

Lifecycle:
  STOPPED → (start) → RUNNING → (tick loop) → RUNNING
  RUNNING → (error) → ERROR
  RUNNING → (stop) → STOPPED
  ERROR → (start) → RUNNING
"""
import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import scoped_session

from ib_trader.data.models import (
    PendingCommand, PendingCommandStatus, BotEvent, Bot,
)
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
from ib_trader.data.repository import TradeRepository, OrderRepository

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class BotBase(ABC):
    """Abstract base class for trading bots.

    Subclasses implement on_tick() with their strategy logic.
    Use self.place_order() to submit commands to the engine.
    Use self.get_open_positions() to read current positions.
    """

    def __init__(self, bot_id: str, config: dict,
                 session_factory: scoped_session) -> None:
        self.bot_id = bot_id
        self.config = config
        self.tick_interval: int = config.get("tick_interval_seconds", 10)

        # Repositories — bot reads/writes SQLite directly
        self._bots = BotRepository(session_factory)
        self._bot_events = BotEventRepository(session_factory)
        self._pending_commands = PendingCommandRepository(session_factory)
        self._trades = TradeRepository(session_factory)
        self._orders = OrderRepository(session_factory)

    @abstractmethod
    async def on_tick(self) -> None:
        """Called every tick_interval_seconds.

        Implement strategy logic here: read market state, decide, act.
        Use self.place_order() to submit orders to the engine.
        """
        ...

    async def on_startup(self, open_positions: list) -> None:
        """Called when the bot starts (or restarts after crash).

        Override to handle crash recovery: check for existing positions
        from a previous incarnation and decide whether to keep or close them.

        Args:
            open_positions: List of TradeGroup objects that were opened by
                            this bot (identified via pending_commands source).
        """
        pass

    async def on_stop(self) -> None:
        """Called when the bot is stopped. Override for cleanup."""
        pass

    # --- Helper Methods ---

    async def place_order(self, command: str, broker: str = "ib") -> str:
        """Submit a command to the engine via pending_commands.

        Args:
            command: Raw command string (e.g., "buy AAPL 10 mid --profit 500").
            broker: Which broker to use.

        Returns:
            Command ID (UUID string) for tracking.
        """
        cmd = PendingCommand(
            source=f"bot:{self.bot_id}",
            broker=broker,
            command_text=command,
            submitted_at=_now_utc(),
        )
        self._pending_commands.insert(cmd)
        self.log_event("ACTION", message=command,
                       payload={"command_id": cmd.id, "broker": broker})
        return cmd.id

    async def wait_for_command(self, cmd_id: str, timeout: float = 60) -> dict | None:
        """Poll pending_commands until the command completes or timeout.

        Args:
            cmd_id: Command ID from place_order().
            timeout: Max seconds to wait.

        Returns:
            Dict with status, output, error — or None on timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cmd = self._pending_commands.get(cmd_id)
            if cmd and cmd.status in (PendingCommandStatus.SUCCESS,
                                       PendingCommandStatus.FAILURE):
                return {
                    "status": cmd.status.value,
                    "output": cmd.output,
                    "error": cmd.error,
                }
            await asyncio.sleep(0.5)
        return None

    async def wait_for_fill(self, trade_serial: int,
                             timeout: float = 120) -> dict | None:
        """Poll trade_groups/orders until the entry order fills or timeout.

        Use after wait_for_command() succeeds — command completion means the
        order was placed, not necessarily filled.

        Returns:
            Trade dict with fill details, or None on timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            trade = self._trades.get_by_serial(trade_serial)
            if trade:
                orders = self._orders.get_for_trade(trade.id)
                entry_orders = [o for o in orders if o.leg_type.value == "ENTRY"]
                if entry_orders and entry_orders[0].status.value in ("FILLED", "PARTIAL"):
                    return {
                        "trade_id": trade.id,
                        "serial": trade.serial_number,
                        "symbol": trade.symbol,
                        "status": trade.status.value,
                        "entry_fill_price": str(entry_orders[0].avg_fill_price),
                        "entry_qty_filled": str(entry_orders[0].qty_filled),
                    }
            await asyncio.sleep(1)
        return None

    def get_open_positions(self) -> list:
        """Read open trade groups from SQLite."""
        return self._trades.get_open()

    def update_signal(self, signal: str) -> None:
        """Update this bot's last_signal in the bots table."""
        self._bots.update_signal(self.bot_id, signal)

    def update_action(self, action: str) -> None:
        """Update this bot's last_action in the bots table."""
        self._bots.update_action(self.bot_id, action)

    def update_heartbeat(self) -> None:
        """Update this bot's heartbeat timestamp."""
        self._bots.update_heartbeat(self.bot_id)

    def log_event(self, event_type: str, message: str | None = None,
                  payload: dict | None = None,
                  trade_serial: int | None = None) -> None:
        """Append an event to the bot_events audit log."""
        self._bot_events.insert(BotEvent(
            bot_id=self.bot_id,
            event_type=event_type,
            message=message,
            payload_json=json.dumps(payload) if payload else None,
            trade_serial=trade_serial,
            recorded_at=_now_utc(),
        ))
