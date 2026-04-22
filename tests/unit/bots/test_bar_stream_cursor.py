"""Tests for the bot bar-stream warmup cursor logic.

Regression coverage for two related bugs:
  1. _read_new_bars starting at "0" consumed stale bars left over from a
     prior run instead of the freshly published warmup bars.
  2. count=500 underfilled typical lookbacks (e.g. 20 bars × 3min = 720
     raw 5s bars).
"""
import pytest

from ib_trader.redis.streams import StreamWriter, StreamNames


@pytest.fixture
async def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def _bar(o, h, l, c, v, ts="2026-04-13T10:00:00+00:00"):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v}


class _FakeBot:
    """Minimal BotBase stand-in exposing only what _read_new_bars needs."""

    def __init__(self, redis):
        self.config = {"_redis": redis}

    async def read_new_bars(self, symbol, count=500):
        from ib_trader.bots.runtime import StrategyBotRunner
        # Bind the real method to our fake instance
        return await StrategyBotRunner._read_new_bars(self, symbol, count=count)


class TestReadNewBarsCount:
    """count parameter must actually be forwarded to XREAD."""

    @pytest.mark.asyncio
    async def test_count_honored(self, fake_redis):
        writer = StreamWriter(fake_redis, StreamNames.bar("QQQ", "5s"), maxlen=10000)
        for i in range(1500):
            await writer.add(_bar(i, i, i, i, i))

        bot = _FakeBot(fake_redis)
        bars = await bot.read_new_bars("QQQ", count=1200)
        assert len(bars) == 1200

    @pytest.mark.asyncio
    async def test_count_default_caps_at_500(self, fake_redis):
        writer = StreamWriter(fake_redis, StreamNames.bar("QQQ", "5s"), maxlen=10000)
        for i in range(800):
            await writer.add(_bar(i, i, i, i, i))

        bot = _FakeBot(fake_redis)
        bars = await bot.read_new_bars("QQQ")
        assert len(bars) == 500


class TestWarmupCursorSkipsStaleBars:
    """Snapshotting the stream's latest ID before warmup skips prior-run bars."""

    @pytest.mark.asyncio
    async def test_reads_only_entries_after_cursor(self, fake_redis):
        stream = StreamNames.bar("QQQ", "5s")
        writer = StreamWriter(fake_redis, stream, maxlen=10000)

        # Stale bars left by a previous run
        for i in range(50):
            await writer.add(_bar(i, i, i, i, i))

        # Bot snapshots cursor at this moment
        latest = await fake_redis.xrevrange(stream, count=1)
        cursor = latest[0][0]

        # New warmup publishes after the cursor
        for i in range(50, 80):
            await writer.add(_bar(i, i, i, i, i))

        bot = _FakeBot(fake_redis)
        bot._last_bar_stream_id = cursor
        bars = await bot.read_new_bars("QQQ", count=500)

        # Only post-cursor bars are returned
        assert len(bars) == 30
        assert bars[0]["open"] == 50.0
        assert bars[-1]["open"] == 79.0

    @pytest.mark.asyncio
    async def test_cursor_defaults_to_zero_when_empty(self, fake_redis):
        stream = StreamNames.bar("QQQ", "5s")
        latest = await fake_redis.xrevrange(stream, count=1)
        assert latest == []

        # On empty stream, cursor stays "0" and reading finds nothing yet
        bot = _FakeBot(fake_redis)
        bot._last_bar_stream_id = "0"
        bars = await bot.read_new_bars("QQQ", count=500)
        assert bars == []
