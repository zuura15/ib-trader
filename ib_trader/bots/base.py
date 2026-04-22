"""Abstract bot base class.

Bots are strategies that run in the bot runner process. They submit
orders through the engine's internal HTTP API (see middleware.py) and
read live state from Redis. They NEVER hold broker connections directly.

Live state lives in Redis (quotes, positions, bot runtime status) and
in IB (orders, fills, broker-held positions). SQLite is archival only
— bot event audit history is written here, but never read to make a
live decision.

Lifecycle:
  STOPPED → (start) → RUNNING → (tick loop) → RUNNING
  RUNNING → (error) → ERROR
  RUNNING → (stop) → STOPPED
  ERROR → (start) → RUNNING
"""
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from sqlalchemy.orm import scoped_session

from ib_trader.data.models import BotEvent
from ib_trader.data.repositories.bot_repository import BotEventRepository
from ib_trader.data.repository import TradeRepository

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class BotBase(ABC):
    """Abstract base class for trading bots.

    Subclasses implement on_tick() with their strategy logic. Orders go
    through the middleware pipeline (ExecutionMiddleware → engine HTTP
    API). Bot event audit log is the only SQLite surface here.
    """

    def __init__(self, bot_id: str, config: dict,
                 session_factory: scoped_session) -> None:
        self.bot_id = bot_id
        self.config = config
        self.tick_interval: int = config.get("tick_interval_seconds", 10)

        # Archival: bot event audit log (CLAUDE.md allows archival writes).
        self._bot_events = BotEventRepository(session_factory)

        # TODO(redis-positions): RiskMiddleware.max_positions still reads
        # open trade groups from SQLite. Migrate to Redis position state
        # or IB positions so this field can go away.
        self._trades = TradeRepository(session_factory)

        # Redis-backed runtime state (status, last_action, heartbeat,
        # kill_switch, error_message). ``config['_redis']`` is populated
        # by the runner from the process-wide client.
        from ib_trader.bots.state import BotStateStore
        self._state = BotStateStore(config.get("_redis"))

    @abstractmethod
    async def on_tick(self) -> None:
        """Called every tick_interval_seconds.

        Implement strategy logic here: read market state, decide, act.
        """
        ...

    async def on_startup(self, open_positions: list) -> None:  # noqa: B027 — optional override
        """Called when the bot starts (or restarts after crash)."""

    async def on_stop(self) -> None:  # noqa: B027 — optional override
        """Called when the bot is stopped."""

    # --- Helper Methods ---

    async def update_action(self, action: str) -> None:
        """Record this bot's last action in Redis."""
        await self._state.set_last_action(self.bot_id, action)

    async def read_last_action(self) -> str | None:
        """Read the last_action string from Redis."""
        data = await self._state.get_last_action(self.bot_id)
        if not data:
            return None
        return data.get("action")

    async def clear_last_action(self) -> None:
        """Clear the Redis last_action key."""
        await self._state.clear_last_action(self.bot_id)

    async def update_heartbeat(self) -> None:
        """Update this bot's heartbeat timestamp in Redis."""
        await self._state.update_heartbeat(self.bot_id)

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
