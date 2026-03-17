"""Tests for bot framework: repositories, base class, registry, runner."""
import pytest
import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import (
    Base, Bot, BotEvent, BotStatus, OrderTemplate,
)
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
from ib_trader.data.repositories.template_repository import OrderTemplateRepository
from ib_trader.bots.registry import register_strategy, get_strategy_class, list_strategies


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def bot_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return scoped_session(sessionmaker(bind=engine))


class TestBotRepository:
    def test_create_and_get(self, bot_session_factory):
        repo = BotRepository(bot_session_factory)
        bot = Bot(
            name="test-bot", strategy="mean_revert", broker="ib",
            created_at=_now(), updated_at=_now(),
        )
        repo.create(bot)
        fetched = repo.get(bot.id)
        assert fetched is not None
        assert fetched.name == "test-bot"
        assert fetched.status == BotStatus.STOPPED

    def test_get_by_name(self, bot_session_factory):
        repo = BotRepository(bot_session_factory)
        repo.create(Bot(name="alpha", strategy="s1", created_at=_now(), updated_at=_now()))
        assert repo.get_by_name("alpha") is not None
        assert repo.get_by_name("beta") is None

    def test_update_status(self, bot_session_factory):
        repo = BotRepository(bot_session_factory)
        bot = Bot(name="bot1", strategy="s1", created_at=_now(), updated_at=_now())
        repo.create(bot)
        repo.update_status(bot.id, BotStatus.RUNNING)
        assert repo.get(bot.id).status == BotStatus.RUNNING

    def test_update_status_with_error(self, bot_session_factory):
        repo = BotRepository(bot_session_factory)
        bot = Bot(name="bot2", strategy="s1", created_at=_now(), updated_at=_now())
        repo.create(bot)
        repo.update_status(bot.id, BotStatus.ERROR, error_message="tick failed")
        fetched = repo.get(bot.id)
        assert fetched.status == BotStatus.ERROR
        assert fetched.error_message == "tick failed"

    def test_update_heartbeat(self, bot_session_factory):
        repo = BotRepository(bot_session_factory)
        bot = Bot(name="bot3", strategy="s1", created_at=_now(), updated_at=_now())
        repo.create(bot)
        assert bot.last_heartbeat is None
        repo.update_heartbeat(bot.id)
        assert repo.get(bot.id).last_heartbeat is not None

    def test_get_by_status(self, bot_session_factory):
        repo = BotRepository(bot_session_factory)
        repo.create(Bot(name="r1", strategy="s1", status=BotStatus.RUNNING,
                        created_at=_now(), updated_at=_now()))
        repo.create(Bot(name="s1", strategy="s1", status=BotStatus.STOPPED,
                        created_at=_now(), updated_at=_now()))
        running = repo.get_by_status(BotStatus.RUNNING)
        assert len(running) == 1
        assert running[0].name == "r1"

    def test_increment_trades(self, bot_session_factory):
        repo = BotRepository(bot_session_factory)
        bot = Bot(name="bot4", strategy="s1", created_at=_now(), updated_at=_now())
        repo.create(bot)
        repo.increment_trades(bot.id)
        repo.increment_trades(bot.id)
        fetched = repo.get(bot.id)
        assert fetched.trades_total == 2
        assert fetched.trades_today == 2

    def test_delete(self, bot_session_factory):
        repo = BotRepository(bot_session_factory)
        bot = Bot(name="del-bot", strategy="s1", created_at=_now(), updated_at=_now())
        repo.create(bot)
        repo.delete(bot.id)
        assert repo.get(bot.id) is None


class TestBotEventRepository:
    def test_insert_and_get(self, bot_session_factory):
        bot_repo = BotRepository(bot_session_factory)
        bot = Bot(name="evbot", strategy="s1", created_at=_now(), updated_at=_now())
        bot_repo.create(bot)

        repo = BotEventRepository(bot_session_factory)
        repo.insert(BotEvent(
            bot_id=bot.id, event_type="STARTED",
            message="Bot started", recorded_at=_now(),
        ))
        repo.insert(BotEvent(
            bot_id=bot.id, event_type="SIGNAL",
            message="BUY signal", payload_json='{"symbol": "AAPL"}',
            recorded_at=_now(),
        ))

        events = repo.get_for_bot(bot.id)
        assert len(events) == 2

    def test_get_by_type(self, bot_session_factory):
        bot_repo = BotRepository(bot_session_factory)
        bot = Bot(name="typebot", strategy="s1", created_at=_now(), updated_at=_now())
        bot_repo.create(bot)

        repo = BotEventRepository(bot_session_factory)
        repo.insert(BotEvent(bot_id=bot.id, event_type="SIGNAL", recorded_at=_now()))
        repo.insert(BotEvent(bot_id=bot.id, event_type="ERROR", recorded_at=_now()))
        repo.insert(BotEvent(bot_id=bot.id, event_type="SIGNAL", recorded_at=_now()))

        signals = repo.get_by_type(bot.id, "SIGNAL")
        assert len(signals) == 2


class TestOrderTemplateRepository:
    def test_create_and_get_all(self, bot_session_factory):
        repo = OrderTemplateRepository(bot_session_factory)
        now = _now()
        repo.create(OrderTemplate(
            label="SPY dip buy", symbol="SPY", side="BUY",
            quantity=Decimal("100"), order_type="LMT",
            price=Decimal("514.00"), created_at=now, updated_at=now,
        ))
        all_templates = repo.get_all()
        assert len(all_templates) == 1
        assert all_templates[0].label == "SPY dip buy"

    def test_delete(self, bot_session_factory):
        repo = OrderTemplateRepository(bot_session_factory)
        now = _now()
        t = OrderTemplate(
            label="del-me", symbol="AAPL", side="BUY",
            quantity=Decimal("50"), order_type="MKT",
            created_at=now, updated_at=now,
        )
        repo.create(t)
        repo.delete(t.id)
        assert repo.get(t.id) is None


class TestBotRegistry:
    def test_register_and_get(self):
        from ib_trader.bots.base import BotBase

        class DummyBot(BotBase):
            async def on_tick(self): pass

        register_strategy("dummy_test", DummyBot)
        assert get_strategy_class("dummy_test") is DummyBot
        assert "dummy_test" in list_strategies()

    def test_unknown_returns_none(self):
        assert get_strategy_class("nonexistent_strategy_xyz") is None
