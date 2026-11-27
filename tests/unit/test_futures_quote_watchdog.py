"""Per-contract futures quote watchdog (ib_resilience).

Incidents 2026-11-25 / 2026-11-27: after an IB farm blip every FUT
stream stopped while stocks kept ticking, so the cross-symbol heartbeat
watchdog never fired. These pin silence detection, Globex hours, the
once-per-episode reconnect policy and the loop wiring.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from ib_trader.engine import ib_resilience as r

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)


def _row(sym, sec_type="FUT", ago=None):
    return {
        "con_id": hash(sym) & 0xFFFF, "symbol": sym, "sec_type": sec_type,
        "last_tick_at": None if ago is None else NOW - timedelta(seconds=ago),
    }


def test_silent_futures_flags_only_ticked_then_silent_fut():
    rows = [
        _row("NQZ6", ago=1800),       # silent FUT → flagged
        _row("MGCG7", ago=90),        # silent FUT → flagged
        _row("ESZ6", ago=2),          # ticking FUT
        _row("QQQ", "STK", ago=5000), # stocks are not this watchdog's job
        _row("GCF7"),                 # never ticked (illiquid) → skipped
    ]
    out = r.silent_futures(rows, NOW, threshold_s=60)
    assert [x["symbol"] for x in out] == ["NQZ6", "MGCG7"]
    assert out[0]["silent_s"] == 1800.0


@pytest.mark.parametrize("ct_dt,expected", [
    (datetime(2026, 11, 27, 3, 18, tzinfo=CT), True),    # Fri overnight
    (datetime(2026, 11, 27, 16, 30, tzinfo=CT), False),  # Fri after close
    (datetime(2026, 11, 28, 12, 0, tzinfo=CT), False),   # Saturday
    (datetime(2026, 11, 29, 16, 59, tzinfo=CT), False),  # Sun before open
    (datetime(2026, 11, 29, 17, 0, tzinfo=CT), True),    # Sun reopen
    (datetime(2026, 11, 30, 16, 15, tzinfo=CT), False),  # Mon daily break
    (datetime(2026, 11, 30, 17, 5, tzinfo=CT), True),    # Mon after break
])
def test_futures_ticks_expected(ct_dt, expected):
    assert r.futures_ticks_expected(ct_dt) is expected


def test_state_reconnects_once_per_episode_then_alerts_then_resolves():
    st = r.FuturesWatchdogState(min_confirmations=2, cooldown_s=0)
    assert st.step(2, 0) == "none"        # unconfirmed
    assert st.step(2, 15) == "reconnect"  # confirmed → reconnect
    st2 = r.FuturesWatchdogState(min_confirmations=1, cooldown_s=0)
    assert st2.step(1, 0) == "reconnect"
    assert st2.step(1, 15) == "alert"     # still dark → no second reconnect
    assert st2.step(1, 30) == "alert"
    assert st2.step(0, 45) == "resolve"   # recovered → clear alert
    assert st2.step(0, 60) == "none"
    assert st2.step(1, 75) == "reconnect" # new episode may reconnect again


def test_state_respects_cooldown_across_episodes():
    st = r.FuturesWatchdogState(min_confirmations=1, cooldown_s=1800)
    assert st.step(1, 0) == "reconnect"
    assert st.step(0, 15) == "resolve"
    assert st.step(1, 600) == "alert"     # within cooldown → alert only
    assert st.step(0, 615) == "resolve"
    assert st.step(1, 1900) == "reconnect"


def test_state_alert_only_when_auto_reconnect_off():
    st = r.FuturesWatchdogState(min_confirmations=1, auto_reconnect=False)
    assert st.step(3, 0) == "alert"


def test_unconfirmed_blip_resets():
    st = r.FuturesWatchdogState(min_confirmations=2)
    assert st.step(1, 0) == "none"
    assert st.step(0, 15) == "none"       # not an episode → nothing to resolve
    assert st.step(1, 30) == "none"


def test_message_names_symbols():
    silent = [{"symbol": "NQZ6", "silent_s": 1800.0}, {"symbol": "GCG7", "silent_s": 90.0}]
    msg = r._fut_watchdog_message(silent, reconnecting=True)
    assert "NQZ6 1800s" in msg and "GCG7 90s" in msg and "Reconnecting" in msg
    assert "Still silent" in r._fut_watchdog_message(silent, reconnecting=False)


@pytest.mark.asyncio
async def test_loop_alerts_and_reconnects_on_silent_futures():
    activity = [_row("NQZ6", ago=1800), _row("QQQ", "STK", ago=0)]
    now_dt = NOW
    ctx = SimpleNamespace(
        settings={
            "futures_watchdog_interval_seconds": 0.001,
            "futures_watchdog_min_confirmations": 1,
            "tick_watchdog_startup_grace_seconds": 0,
        },
        ib=SimpleNamespace(is_connected=lambda: True, streaming_activity=lambda: activity),
        redis=None,
    )
    alert = AsyncMock()
    reconnected = asyncio.Event()

    async def fake_reconnect(_ctx, *, source):
        assert source == "futures_watchdog"
        reconnected.set()
        return SimpleNamespace(reconnect_ok=True, reconnect_error=None)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_dt.astimezone(tz) if tz else now_dt

    with patch.object(r, "reconnect_ib", fake_reconnect), \
         patch.object(r, "futures_ticks_expected", lambda: True), \
         patch.object(r, "datetime", _FixedDT), \
         patch("ib_trader.logging_.alerts.log_and_alert", alert):
        task = asyncio.create_task(r.futures_quote_watchdog_loop(ctx))
        await asyncio.wait_for(reconnected.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    kwargs = alert.await_args.kwargs
    assert kwargs["trigger"] == r.FUT_WATCHDOG_TRIGGER
    assert kwargs["severity"] == "WARNING"
    assert "NQZ6" in kwargs["message"]


@pytest.mark.asyncio
async def test_loop_skips_when_disconnected():
    calls = {"n": 0}

    def activity():
        calls["n"] += 1
        return []

    ctx = SimpleNamespace(
        settings={"futures_watchdog_interval_seconds": 0.001,
                  "tick_watchdog_startup_grace_seconds": 0},
        ib=SimpleNamespace(is_connected=lambda: False, streaming_activity=activity),
        redis=None,
    )
    task = asyncio.create_task(r.futures_quote_watchdog_loop(ctx))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_loop_disabled_with_zero_interval():
    ctx = SimpleNamespace(settings={"futures_watchdog_interval_seconds": 0},
                          ib=SimpleNamespace(), redis=None)
    await asyncio.wait_for(r.futures_quote_watchdog_loop(ctx), timeout=1)


@pytest.mark.asyncio
async def test_resolve_alert_hdels_dedup_id():
    import uuid
    redis = SimpleNamespace(hdel=AsyncMock(return_value=1))
    with patch("ib_trader.redis.streams.publish_activity", AsyncMock()) as pub:
        await r._resolve_alert(redis, r.FUT_WATCHDOG_TRIGGER, r.FUT_WATCHDOG_DEDUP_KEY)
    expected = str(uuid.uuid5(uuid.NAMESPACE_DNS,
                              f"{r.FUT_WATCHDOG_TRIGGER}|{r.FUT_WATCHDOG_DEDUP_KEY}"))
    assert redis.hdel.await_args.args[1] == expected
    pub.assert_awaited_once()


def test_insync_streaming_activity_reports_last_tick():
    from ib_trader.ib.insync_client import InsyncClient, _display_symbol
    ts = datetime(2026, 11, 27, 9, 0)  # naive, as ib_async may hand back
    contract = SimpleNamespace(localSymbol="", symbol="GC", secType="FUT",
                               lastTradeDateOrContractMonth="20270224")
    client = InsyncClient.__new__(InsyncClient)
    client._streaming = {765079322: {"ticker": SimpleNamespace(time=ts), "contract": contract}}
    rows = client.streaming_activity()
    assert rows == [{"con_id": 765079322, "symbol": "GC 202702", "sec_type": "FUT",
                     "last_tick_at": ts.replace(tzinfo=timezone.utc)}]
    assert _display_symbol(SimpleNamespace(localSymbol="NQZ6")) == "NQZ6"
    assert _display_symbol(None) is None
