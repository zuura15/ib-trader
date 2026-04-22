"""Tests for the engine bar publisher that bridges IB reqRealTimeBars to Redis."""
import pytest
from datetime import datetime, timezone

from ib_trader.engine.main import _make_bar_publisher
from ib_trader.redis.streams import StreamNames, StreamReader


@pytest.fixture
async def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class TestBarPublisher:
    @pytest.mark.asyncio
    async def test_publishes_with_short_keys_matching_consumer(self, fake_redis):
        publish = _make_bar_publisher(fake_redis, "QQQ")
        bar_data = {
            "time": datetime(2026, 4, 12, 10, 30, 0, tzinfo=timezone.utc),
            "open": 494.10,
            "high": 494.52,
            "low": 494.05,
            "close": 494.28,
            "volume": 12345,
        }
        await publish(bar_data)

        result = await StreamReader.read_latest(fake_redis, StreamNames.bar("QQQ", "5s"))
        assert result is not None
        _entry_id, data = result
        assert data["o"] == 494.10
        assert data["h"] == 494.52
        assert data["l"] == 494.05
        assert data["c"] == 494.28
        assert data["v"] == 12345
        assert "2026-04-12" in data["ts"]

    @pytest.mark.asyncio
    async def test_non_datetime_timestamp_is_stringified(self, fake_redis):
        publish = _make_bar_publisher(fake_redis, "AAPL")
        await publish({
            "time": "2026-04-12 10:30:00",
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0,
        })
        result = await StreamReader.read_latest(fake_redis, StreamNames.bar("AAPL", "5s"))
        assert result is not None
        _, data = result
        assert data["ts"] == "2026-04-12 10:30:00"

    @pytest.mark.asyncio
    async def test_missing_fields_default_to_zero(self, fake_redis):
        publish = _make_bar_publisher(fake_redis, "SPY")
        await publish({"time": datetime(2026, 4, 12, tzinfo=timezone.utc)})
        result = await StreamReader.read_latest(fake_redis, StreamNames.bar("SPY", "5s"))
        assert result is not None
        _, data = result
        assert data["o"] == 0.0
        assert data["v"] == 0

    @pytest.mark.asyncio
    async def test_separate_streams_per_symbol(self, fake_redis):
        p_qqq = _make_bar_publisher(fake_redis, "QQQ")
        p_spy = _make_bar_publisher(fake_redis, "SPY")
        bar = {
            "time": datetime(2026, 4, 12, tzinfo=timezone.utc),
            "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
        }
        await p_qqq(bar)
        await p_spy(bar)

        qqq_len = await fake_redis.xlen(StreamNames.bar("QQQ", "5s"))
        spy_len = await fake_redis.xlen(StreamNames.bar("SPY", "5s"))
        assert qqq_len == 1
        assert spy_len == 1
