"""Repository for bot configuration and status persistence."""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import scoped_session, Session

from ib_trader.data.models import Bot, BotEvent, BotStatus

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class BotRepository:
    """SQLAlchemy repository for Bot persistence."""

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def create(self, bot: Bot) -> Bot:
        """Insert a new bot and return it."""
        s = self._session()
        s.add(bot)
        s.commit()
        return bot

    def get(self, bot_id: str) -> Bot | None:
        """Return the bot with the given ID, or None."""
        return self._session().query(Bot).filter(Bot.id == bot_id).first()

    def get_by_name(self, name: str) -> Bot | None:
        """Return the bot with the given name, or None."""
        return self._session().query(Bot).filter(Bot.name == name).first()

    def get_all(self) -> list[Bot]:
        """Return all bots ordered by name."""
        return self._session().query(Bot).order_by(Bot.name).all()

    def get_by_status(self, status: BotStatus) -> list[Bot]:
        """Return all bots with the given status."""
        return self._session().query(Bot).filter(Bot.status == status).all()

    def update_status(self, bot_id: str, status: BotStatus,
                      error_message: str | None = None) -> None:
        """Update the status and optionally error_message of a bot."""
        s = self._session()
        bot = s.query(Bot).filter(Bot.id == bot_id).first()
        if bot is None:
            logger.warning('{"event": "BOT_NOT_FOUND", "bot_id": "%s"}', bot_id)
            return
        bot.status = status
        bot.error_message = error_message
        bot.updated_at = _now_utc()
        if status == BotStatus.ERROR and error_message:
            logger.warning('{"event": "BOT_ERROR", "bot_id": "%s", "error": "%s"}',
                           bot_id, error_message[:200])
        s.commit()

    def update_heartbeat(self, bot_id: str) -> None:
        """Update the last_heartbeat timestamp."""
        s = self._session()
        bot = s.query(Bot).filter(Bot.id == bot_id).first()
        if bot:
            bot.last_heartbeat = _now_utc()
            bot.updated_at = _now_utc()
            s.commit()

    def update_signal(self, bot_id: str, signal: str) -> None:
        """Update the last_signal field."""
        s = self._session()
        bot = s.query(Bot).filter(Bot.id == bot_id).first()
        if bot:
            bot.last_signal = signal
            bot.updated_at = _now_utc()
            s.commit()

    def update_action(self, bot_id: str, action: str) -> None:
        """Update the last_action field and timestamp."""
        s = self._session()
        bot = s.query(Bot).filter(Bot.id == bot_id).first()
        if bot:
            bot.last_action = action
            bot.last_action_at = _now_utc()
            bot.updated_at = _now_utc()
            s.commit()

    def update_action_raw(self, bot_id: str, action: str | None) -> None:
        """Set last_action to an arbitrary value (including None) without updating last_action_at."""
        s = self._session()
        bot = s.query(Bot).filter(Bot.id == bot_id).first()
        if bot:
            bot.last_action = action
            bot.updated_at = _now_utc()
            s.commit()

    def increment_trades(self, bot_id: str) -> None:
        """Atomically increment trades_total and trades_today counters.

        Uses SQL-level increment to avoid lost-update race conditions
        when the bot task and runner poll concurrently.
        """
        s = self._session()
        s.query(Bot).filter(Bot.id == bot_id).update({
            Bot.trades_total: Bot.trades_total + 1,
            Bot.trades_today: Bot.trades_today + 1,
            Bot.updated_at: _now_utc(),
        })
        s.commit()

    def update_pnl(self, bot_id: str, pnl_today) -> None:
        """Update pnl_today."""
        s = self._session()
        bot = s.query(Bot).filter(Bot.id == bot_id).first()
        if bot:
            bot.pnl_today = pnl_today
            bot.updated_at = _now_utc()
            s.commit()

    def delete(self, bot_id: str) -> None:
        """Delete a bot by ID."""
        s = self._session()
        bot = s.query(Bot).filter(Bot.id == bot_id).first()
        if bot:
            s.delete(bot)
            s.commit()


class BotEventRepository:
    """Append-only repository for bot events."""

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def insert(self, event: BotEvent) -> None:
        """Persist a new bot event."""
        s = self._session()
        s.add(event)
        s.commit()

    def get_for_bot(self, bot_id: str, limit: int = 100) -> list[BotEvent]:
        """Return recent events for a bot, newest first."""
        return (
            self._session()
            .query(BotEvent)
            .filter(BotEvent.bot_id == bot_id)
            .order_by(BotEvent.recorded_at.desc())
            .limit(limit)
            .all()
        )

    def get_by_type(self, bot_id: str, event_type: str,
                    limit: int = 50) -> list[BotEvent]:
        """Return events of a specific type for a bot."""
        return (
            self._session()
            .query(BotEvent)
            .filter(BotEvent.bot_id == bot_id, BotEvent.event_type == event_type)
            .order_by(BotEvent.recorded_at.desc())
            .limit(limit)
            .all()
        )

    def get_recent(self, limit: int = 50) -> list[BotEvent]:
        """Return recent events across all bots, newest first."""
        return (
            self._session()
            .query(BotEvent)
            .order_by(BotEvent.recorded_at.desc())
            .limit(limit)
            .all()
        )
