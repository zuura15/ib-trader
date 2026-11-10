"""_stream_quote_to_ws sends the newest quote immediately on subscribe."""
import asyncio
import json

from ib_trader.api.ws import _stream_quote_to_ws


class _Redis:
    def __init__(self, newest):
        self.newest = newest
        self.xread_ids: list[str] = []

    async def xrevrange(self, stream, count=1):
        return self.newest

    async def xread(self, streams, block=0):
        self.xread_ids.append(next(iter(streams.values())))
        await asyncio.sleep(3600)


class _WS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


async def _run(redis, ws):
    task = asyncio.create_task(_stream_quote_to_ws(ws, redis, "GCZ6"))
    await asyncio.sleep(0.01)
    task.cancel()


async def test_newest_quote_sent_on_subscribe_then_tail_from_it():
    redis = _Redis([("1700-0", {"last": '"4149.5"', "bid": '"4149.4"'})])
    ws = _WS()
    await _run(redis, ws)
    assert ws.sent == [{
        "type": "quote", "symbol": "GCZ6",
        "data": {"last": "4149.5", "bid": "4149.4"},
    }]
    assert redis.xread_ids == ["1700-0"]      # no gap, no duplicate


async def test_empty_stream_tails_new_ticks_only():
    redis = _Redis([])
    ws = _WS()
    await _run(redis, ws)
    assert ws.sent == []
    assert redis.xread_ids == ["$"]
