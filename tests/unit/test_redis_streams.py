"""Tests for Redis stream and state abstractions."""
import pytest
from datetime import datetime
from decimal import Decimal

from ib_trader.redis.streams import (
    StreamWriter, StreamReader, StreamNames, _serialize, _deserialize,
    publish_activity,
)
from ib_trader.redis.state import StateStore, StateKeys


@pytest.fixture
async def fake_redis():
    """Create a fakeredis instance for testing."""
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class TestSerialization:
    """Tests for stream data serialization."""

    def test_decimal_serialized_as_string(self):
        result = _serialize({"price": Decimal("494.28")})
        assert result["price"] == '"494.28"'

    def test_datetime_serialized_as_iso(self):
        dt = datetime(2026, 4, 12, 10, 30, 0)
        result = _serialize({"ts": dt})
        assert "2026-04-12" in result["ts"]

    def test_round_trip(self):
        original = {"price": Decimal("494.28"), "qty": 20, "symbol": "QQQ"}
        serialized = _serialize(original)
        deserialized = _deserialize(serialized)
        assert deserialized["price"] == "494.28"  # Decimal becomes string
        assert deserialized["qty"] == 20
        assert deserialized["symbol"] == "QQQ"


class TestStreamWriter:
    """Tests for StreamWriter."""

    @pytest.mark.asyncio
    async def test_add_returns_entry_id(self, fake_redis):
        writer = StreamWriter(fake_redis, "test:stream", maxlen=100)
        entry_id = await writer.add({"key": "value"})
        assert entry_id is not None
        assert "-" in entry_id  # Redis stream IDs have format "timestamp-sequence"

    @pytest.mark.asyncio
    async def test_add_multiple_entries(self, fake_redis):
        writer = StreamWriter(fake_redis, "test:stream", maxlen=100)
        id1 = await writer.add({"a": 1})
        id2 = await writer.add({"b": 2})
        assert id1 != id2

        # Verify stream length
        length = await fake_redis.xlen("test:stream")
        assert length == 2


class TestStreamReader:
    """Tests for StreamReader."""

    @pytest.mark.asyncio
    async def test_read_latest_returns_none_for_empty_stream(self, fake_redis):
        result = await StreamReader.read_latest(fake_redis, "empty:stream")
        assert result is None

    @pytest.mark.asyncio
    async def test_read_latest_returns_most_recent(self, fake_redis):
        writer = StreamWriter(fake_redis, "test:stream", maxlen=100)
        await writer.add({"val": "first"})
        await writer.add({"val": "second"})

        result = await StreamReader.read_latest(fake_redis, "test:stream")
        assert result is not None
        _entry_id, data = result
        assert data["val"] == "second"


class TestStateStore:
    """Tests for StateStore."""

    @pytest.mark.asyncio
    async def test_set_and_get(self, fake_redis):
        store = StateStore(fake_redis)
        await store.set("test:key", {"state": "OPEN", "qty": 20})
        result = await store.get("test:key")
        assert result == {"state": "OPEN", "qty": 20}

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, fake_redis):
        store = StateStore(fake_redis)
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_decimal_round_trip(self, fake_redis):
        store = StateStore(fake_redis)
        await store.set("test:key", {"price": Decimal("494.28")})
        result = await store.get("test:key")
        assert result["price"] == "494.28"  # Decimal stored as string

    @pytest.mark.asyncio
    async def test_set_with_ttl(self, fake_redis):
        store = StateStore(fake_redis)
        await store.set("test:key", {"val": 1}, ttl=60)
        ttl = await fake_redis.ttl("test:key")
        assert ttl > 0

    @pytest.mark.asyncio
    async def test_delete(self, fake_redis):
        store = StateStore(fake_redis)
        await store.set("test:key", {"val": 1})
        await store.delete("test:key")
        result = await store.get("test:key")
        assert result is None


class TestStreamNames:
    """Tests for stream name constants."""

    def test_quote(self):
        assert StreamNames.quote("QQQ") == "quote:QQQ"

    def test_bar(self):
        assert StreamNames.bar("QQQ", "5s") == "bar:QQQ:5s"

    def test_fill(self):
        assert StreamNames.fill("saw-rsi") == "fill:saw-rsi"

    def test_position_changes(self):
        assert StreamNames.position_changes() == "position:changes"

    def test_bot_event(self):
        assert StreamNames.bot_event("abc123") == "bot:event:abc123"

    def test_alert(self):
        assert StreamNames.alert("CATASTROPHIC") == "alert:CATASTROPHIC"

    def test_bot_control(self):
        assert StreamNames.bot_control("abc123") == "bot:control:abc123"

    def test_activity_constant(self):
        assert StreamNames.ACTIVITY == "events:activity"


class TestPublishActivity:
    """publish_activity nudges the events:activity stream for WS consumers."""

    @pytest.mark.asyncio
    async def test_publishes_channel_name(self, fake_redis):
        await publish_activity(fake_redis, "orders")
        result = await StreamReader.read_latest(fake_redis, StreamNames.ACTIVITY)
        assert result is not None
        _, data = result
        assert data["channel"] == "orders"

    @pytest.mark.asyncio
    async def test_no_redis_is_noop(self):
        # Must not raise when redis is None
        await publish_activity(None, "anything")

    @pytest.mark.asyncio
    async def test_redis_error_swallowed(self):
        class _BrokenRedis:
            async def xadd(self, *args, **kwargs):
                raise RuntimeError("redis down")
        # Should not raise — activity is observational, never gating
        await publish_activity(_BrokenRedis(), "orders")


class TestStateKeys:
    """Tests for state key constants."""

    def test_quote_latest(self):
        assert StateKeys.quote_latest("QQQ") == "quote:QQQ:latest"

    def test_position(self):
        assert StateKeys.position("saw-rsi", "QQQ") == "pos:saw-rsi:QQQ"

    def test_strategy(self):
        assert StateKeys.strategy("saw-rsi", "QQQ") == "strat:saw-rsi:QQQ"

    def test_bot_status(self):
        assert StateKeys.bot_status("abc") == "bot:abc:status"

    def test_heartbeat(self):
        assert StateKeys.heartbeat("engine") == "hb:engine"
