"""IB session resilience: tick-silence watchdog + scheduled reconnect.

Two background tasks plus a reusable reconnect helper. Designed to be
boring on the happy path — the engine pumps ticks, the watchdog sleeps,
the schedule waits — and noisy *exactly when something is wrong*.

Background:
    On 2026-05-27 the engine's ``pendingTickersEvent`` callback stopped
    firing for every subscribed STK + FUT at 14:45 PT. The IB socket
    stayed alive (positionEvent + orderStatus still fired), so neither
    ``_reconnect_with_backoff`` nor the existing /api/status heartbeat
    noticed. Bots, charts, and the positions overlay sat on cached
    numbers for hours with no alert. We had no signal because the
    httpcore DEBUG flood had rotated every engine event off disk.

What this module adds:

  * :func:`tick_silence_watchdog_loop` polls ``quotes:heartbeat`` every
    N seconds. When silence exceeds a threshold *during a session window
    where ticks are expected*, it dumps ib_async's current ticker state
    to the log (per-symbol ``silent_s``, bid/ask/last, connState) and
    fires a CATASTROPHIC alert. Debounced so one stall doesn't storm
    the alerts pane.

  * :func:`reconnect_ib` is a reusable helper that drops the IB socket,
    reconnects via the same code path as engine startup, then re-runs
    every watchlist + position market-data subscription. Used by both
    the manual ``POST /engine/ib/reconnect`` endpoint and the daily
    scheduler below.

  * :func:`scheduled_reconnect_loop` reconnects once per day at 02:30
    PT — fifteen minutes after the IB Gateway's own 02:15 restart
    window. Cadence is set in ``settings.yaml`` (``ib_daily_reconnect_pt``).
    A pre-flight check skips the cycle when ticks are flowing normally
    on the assumption "if it ain't broke", but the cycle still runs at
    least once every 30 h to bound any drift the watchdog might miss.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from ib_trader.engine.main import AppContext

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")


# Tunables — kept module-local; overridable via settings.yaml.
DEFAULT_SILENCE_THRESHOLD_S = 60
DEFAULT_WATCHDOG_INTERVAL_S = 15
DEFAULT_STARTUP_GRACE_S = 120        # no alerts in the first 2 min after loop start
DEFAULT_MIN_CONFIRMATIONS = 3        # require 3 consecutive silent polls before alerting
DEFAULT_RECONNECT_PT = "02:30"       # daily reconnect time
DEFAULT_RECONNECT_MAX_GAP_HOURS = 30 # force a cycle if none happened in this window
DEFAULT_PROPHYLACTIC_INTERVAL_H = 1.0
DEFAULT_PROPHYLACTIC_STAGGER_S = 2.0
DEFAULT_PROPHYLACTIC_STARTUP_DELAY_S = 600  # 10 min — let the first session settle


# ---------------------------------------------------------------------------
# Reconnect helper (shared by manual endpoint + scheduler)
# ---------------------------------------------------------------------------

@dataclass
class ReconnectResult:
    """Structured summary returned by :func:`reconnect_ib`."""
    started_at: str
    finished_at: str
    duration_s: float
    pre_reconnect_silence_s: float | None
    disconnect_ok: bool
    reconnect_ok: bool
    reconnect_error: str | None
    watchlist_resubscribed: int
    watchlist_failed: int
    positions_resubscribed: int
    positions_failed: int


async def reconnect_ib(ctx: "AppContext", *, source: str) -> ReconnectResult:
    """Drop the IB socket and reconnect, then re-issue all market-data subs.

    Mirrors the startup connect + subscribe sequence so a successful
    reconnect leaves the engine in the same state as a fresh ``make dev``
    boot, minus order-ledger rebuild (which fires automatically via the
    reconciler on next pass).

    ``source`` is recorded in every log line for postmortem ("scheduled",
    "manual", "watchdog_recovery", ...). Errors are caught individually
    so a single failed re-subscribe doesn't abort the whole cycle.
    """
    from ib_trader.config.loader import load_watchlist
    from ib_trader.repl.commands import _is_futures_local_symbol
    from ib_trader.redis.state import StateKeys

    started_dt = datetime.now(PT)
    started_mono = asyncio.get_event_loop().time()

    pre_silence = await _heartbeat_silence_seconds(ctx)

    logger.info(
        '{"event": "IB_RECONNECT_STARTED", "source": "%s", '
        '"pre_silence_s": %s}',
        source,
        "null" if pre_silence is None else f"{pre_silence:.1f}",
    )

    disconnect_ok = False
    try:
        await ctx.ib.disconnect()
        disconnect_ok = True
    except Exception:
        logger.exception('{"event": "IB_RECONNECT_DISCONNECT_FAILED", "source": "%s"}', source)

    # Brief settle so ib_async's disconnectedEvent fires before we re-open.
    await asyncio.sleep(1.0)

    reconnect_ok = False
    reconnect_err: str | None = None
    try:
        await ctx.ib.connect()
        reconnect_ok = True
    except Exception as e:
        reconnect_err = repr(e)
        logger.exception('{"event": "IB_RECONNECT_CONNECT_FAILED", "source": "%s"}', source)

    watchlist_ok = 0
    watchlist_fail = 0
    if reconnect_ok:
        try:
            symbols = load_watchlist("config/watchlist.yaml")
        except Exception:
            logger.exception('{"event": "IB_RECONNECT_WATCHLIST_LOAD_FAILED", "source": "%s"}', source)
            symbols = []
        for sym in symbols:
            try:
                sec_type = "FUT" if _is_futures_local_symbol(sym) else "STK"
                info = await ctx.ib.qualify_contract(sym, sec_type=sec_type)
                # count_ref=False — watchlist is ambient lifetime (no paired
                # unsub). Matches the startup-path semantics.
                await ctx.ib.subscribe_market_data(
                    info["con_id"], sym, count_ref=False,
                )
                watchlist_ok += 1
            except Exception:
                watchlist_fail += 1
                logger.warning(
                    '{"event": "IB_RECONNECT_WATCHLIST_SUB_FAILED", '
                    '"source": "%s", "symbol": "%s"}', source, sym,
                )

    positions_ok = 0
    positions_fail = 0
    if reconnect_ok:
        # _refresh_positions_cache(..., subscribe_mktdata=True) is what
        # the startup path uses; re-running it here re-issues subs for
        # every held STK/FUT (OPT positions are skipped by the publisher).
        try:
            from ib_trader.engine.main import _refresh_positions_cache
            held = await _refresh_positions_cache(ctx, subscribe_mktdata=True)
            # We don't get a per-position success/fail from the helper;
            # record total positions touched so the postmortem shows
            # which side of the world we re-anchored.
            positions_ok = held
        except Exception:
            positions_fail = 1
            logger.exception('{"event": "IB_RECONNECT_POSITION_REFRESH_FAILED", "source": "%s"}', source)

    # Touch the heartbeat key explicitly so downstream consumers see a
    # fresh "alive" signal even before the first post-reconnect tick
    # propagates — avoids a spurious watchdog alert immediately after a
    # successful reconnect cycle.
    if reconnect_ok and ctx.redis is not None:
        try:
            from ib_trader.redis.state import StateStore
            store = StateStore(ctx.redis)
            await store.set(
                StateKeys.quotes_heartbeat(),
                {"ts": datetime.now(PT).astimezone().isoformat(),
                 "source": f"reconnect:{source}"},
                ttl=StateKeys.QUOTES_HEARTBEAT_TTL,
            )
        except Exception:
            logger.debug("post-reconnect heartbeat refresh failed", exc_info=True)

    finished_dt = datetime.now(PT)
    duration = asyncio.get_event_loop().time() - started_mono

    result = ReconnectResult(
        started_at=started_dt.isoformat(),
        finished_at=finished_dt.isoformat(),
        duration_s=round(duration, 2),
        pre_reconnect_silence_s=(
            round(pre_silence, 1) if pre_silence is not None else None
        ),
        disconnect_ok=disconnect_ok,
        reconnect_ok=reconnect_ok,
        reconnect_error=reconnect_err,
        watchlist_resubscribed=watchlist_ok,
        watchlist_failed=watchlist_fail,
        positions_resubscribed=positions_ok,
        positions_failed=positions_fail,
    )

    logger.info(
        '{"event": "IB_RECONNECT_FINISHED", "source": "%s", '
        '"duration_s": %.2f, "reconnect_ok": %s, '
        '"watchlist_resubscribed": %d, "watchlist_failed": %d, '
        '"positions_resubscribed": %d, "positions_failed": %d, '
        '"pre_silence_s": %s}',
        source, result.duration_s, "true" if reconnect_ok else "false",
        watchlist_ok, watchlist_fail, positions_ok, positions_fail,
        "null" if pre_silence is None else f"{pre_silence:.1f}",
    )

    return result


# ---------------------------------------------------------------------------
# Tick-silence watchdog
# ---------------------------------------------------------------------------

async def tick_silence_watchdog_loop(ctx: "AppContext") -> None:
    """Detect quote-stream silence and surface it loudly (once).

    Polls every ``interval_s`` seconds. When the silence exceeds
    ``threshold_s`` AND we expect ticks (session window check), dump
    rich ib_async state and fire a single dedup'd WARNING alert
    (overwrites the same row on every re-fire — see ``dedup_key`` in
    ``log_and_alert``). Severity is WARNING because the symptom is
    "trading data may be stale" — important to flag, but not "halt
    the universe behind a blocking modal."

    Two startup-grace mechanisms protect against false positives:

      * ``startup_grace_s`` — no alerts at all for the first N seconds
        after this loop starts, so the engine's own subscribe-watchlist
        / refresh-positions sequence has room to publish the first
        heartbeat.
      * ``min_confirmations`` — N consecutive silence detections needed
        before alerting. Suppresses single-poll glitches and the
        "fresh-start, heartbeat key not yet written" race.
    """
    interval_s = float(ctx.settings.get(
        "tick_watchdog_interval_seconds", DEFAULT_WATCHDOG_INTERVAL_S,
    ))
    threshold_s = float(ctx.settings.get(
        "tick_watchdog_silence_threshold_seconds", DEFAULT_SILENCE_THRESHOLD_S,
    ))
    startup_grace_s = float(ctx.settings.get(
        "tick_watchdog_startup_grace_seconds", DEFAULT_STARTUP_GRACE_S,
    ))
    min_confirmations = int(ctx.settings.get(
        "tick_watchdog_min_confirmations", DEFAULT_MIN_CONFIRMATIONS,
    ))

    loop_started_mono = asyncio.get_event_loop().time()
    consecutive_silent = 0

    while True:
        try:
            await asyncio.sleep(interval_s)

            # Startup grace: don't even evaluate for the first N seconds.
            if (asyncio.get_event_loop().time() - loop_started_mono) < startup_grace_s:
                continue

            silence = await _heartbeat_silence_seconds(ctx)
            if silence is None:
                # Heartbeat key missing → treat as a long silence (the TTL
                # already expired, or the publisher hasn't published yet).
                silence = float("inf")

            if silence <= threshold_s:
                consecutive_silent = 0
                continue

            if not _ticks_expected_now():
                consecutive_silent = 0
                logger.debug(
                    '{"event": "TICK_WATCHDOG_SILENCE_IGNORED", '
                    '"silence_s": %s, "reason": "ticks_not_expected"}',
                    "null" if silence == float("inf") else f"{silence:.1f}",
                )
                continue

            consecutive_silent += 1
            if consecutive_silent < min_confirmations:
                logger.debug(
                    '{"event": "TICK_WATCHDOG_SILENCE_UNCONFIRMED", '
                    '"consecutive": %d, "required": %d}',
                    consecutive_silent, min_confirmations,
                )
                continue

            # Snapshot ib_async state for postmortem before alerting.
            snapshot = _capture_ib_state(ctx)
            silence_display = (
                "60s+" if silence == float("inf") else f"{silence:.0f}s"
            )
            logger.error(
                '{"event": "TICK_WATCHDOG_SILENCE_DETECTED", '
                '"silence_s": %s, "consecutive": %d, "ib_state": %s}',
                "null" if silence == float("inf") else f"{silence:.1f}",
                consecutive_silent, json.dumps(snapshot, default=str),
            )

            try:
                from ib_trader.logging_.alerts import log_and_alert
                await log_and_alert(
                    redis=ctx.redis,
                    trigger="TICK_STREAM_SILENT",
                    message=(
                        f"Quote stream silent ≥{silence_display}. "
                        f"IB connState={snapshot.get('connState')}, "
                        f"tickers held={snapshot.get('ticker_count')}. "
                        "Trading bots may be on stale prices. "
                        "Try POST /api/system/ib/reconnect to recover."
                    ),
                    severity="WARNING",  # amber banner, not blocking modal
                    exc_info=False,
                    # Stable id — every re-fire overwrites the same row
                    # instead of piling up new alerts.
                    dedup_key="watchdog:tick-stream",
                )
            except Exception:
                logger.exception('{"event": "TICK_WATCHDOG_ALERT_FAILED"}')
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('{"event": "TICK_WATCHDOG_LOOP_ERROR"}')
            # Don't tight-loop on persistent failure.
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Scheduled daily reconnect
# ---------------------------------------------------------------------------

async def scheduled_reconnect_loop(ctx: "AppContext") -> None:
    """Reconnect once per day at the configured PT wall-clock time.

    Default is 02:30 PT — 15 min after the IB Gateway's own 02:15
    restart window — so we land on a fresh upstream session. The HH:MM
    string in ``settings.yaml`` (``ib_daily_reconnect_pt``) overrides.

    A safety floor (``ib_daily_reconnect_max_gap_hours``, default 30 h)
    triggers a reconnect even if the wall-clock hit was missed (laptop
    sleep, settings-yaml reload during the firing window, etc.) — the
    cycle should never go > 30 h without running.
    """
    schedule_str: str = str(ctx.settings.get(
        "ib_daily_reconnect_pt", DEFAULT_RECONNECT_PT,
    ))
    max_gap_h = float(ctx.settings.get(
        "ib_daily_reconnect_max_gap_hours", DEFAULT_RECONNECT_MAX_GAP_HOURS,
    ))
    try:
        sched_h, sched_m = (int(p) for p in schedule_str.split(":", 1))
    except Exception:
        logger.error(
            '{"event": "IB_DAILY_RECONNECT_BAD_CONFIG", '
            '"value": "%s", "fallback": "%s"}',
            schedule_str, DEFAULT_RECONNECT_PT,
        )
        sched_h, sched_m = (int(p) for p in DEFAULT_RECONNECT_PT.split(":"))

    last_run: datetime | None = None
    logger.info(
        '{"event": "IB_DAILY_RECONNECT_SCHEDULED", '
        '"time_pt": "%02d:%02d", "max_gap_hours": %.1f}',
        sched_h, sched_m, max_gap_h,
    )

    while True:
        try:
            now = datetime.now(PT)
            next_fire = now.replace(hour=sched_h, minute=sched_m,
                                    second=0, microsecond=0)
            if next_fire <= now:
                next_fire = next_fire + timedelta(days=1)

            # Force-fire if the inter-cycle floor was breached. Useful
            # when the wall-clock target was missed (process started after
            # 02:30, laptop slept through the window, etc.).
            if last_run is not None:
                forced_at = last_run + timedelta(hours=max_gap_h)
                if forced_at < next_fire:
                    next_fire = max(forced_at, now + timedelta(seconds=10))

            sleep_s = max(1.0, (next_fire - now).total_seconds())
            logger.info(
                '{"event": "IB_DAILY_RECONNECT_SLEEPING_UNTIL", '
                '"target_pt": "%s", "sleep_s": %.0f}',
                next_fire.isoformat(), sleep_s,
            )
            await asyncio.sleep(sleep_s)

            result = await reconnect_ib(ctx, source="scheduled")
            last_run = datetime.now(PT)
            if not result.reconnect_ok:
                # The standard engine reconnect-with-backoff loop should
                # already be running by now (ib_async fires
                # disconnectedEvent on the disconnect we just did, which
                # triggers _reconnect_with_backoff). Log and continue —
                # the next scheduled cycle will retry from scratch.
                logger.warning(
                    '{"event": "IB_DAILY_RECONNECT_INCOMPLETE", '
                    '"error": %s}',
                    json.dumps(result.reconnect_error),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('{"event": "IB_DAILY_RECONNECT_LOOP_ERROR"}')
            # Don't tight-loop on a persistent failure; wait an hour
            # before re-arming the wall-clock calculation.
            await asyncio.sleep(3600)


async def prophylactic_resubscribe_loop(ctx: "AppContext") -> None:
    """Every N hours, cancel + re-issue every market-data subscription.

    Addresses the "IB silently parks idle subscriptions" failure mode
    (operator-reported pattern, 2026-06-02). The TCP socket stays open,
    no error fires, our dicts still say "subscribed", but IB stops
    pushing ticks for some symbols. A fresh ``reqMktData`` unsticks
    IB's parked state because from its side this is a brand-new
    subscription. We do the same thing a Gateway restart does — per
    symbol, without dropping the socket.

    Cadence is set in ``settings.yaml`` (``prophylactic_resub_interval_hours``,
    default 1). A startup delay (``prophylactic_resub_startup_delay_seconds``,
    default 600) lets the engine's first watchlist + positions subscribe
    settle before we start cycling. Stagger between symbols
    (``prophylactic_resub_stagger_seconds``, default 2) bounds per-symbol
    data gaps to ~250 ms each and avoids a thundering-herd reqMktData
    burst at IB.

    Raises a WARNING ``IB_PROPHYLACTIC_RESUB_INFLIGHT`` alert at start
    and resolves it on completion. ``bots.runtime.check_stale_quote``
    consults this trigger and suppresses per-bot STALE_QUOTES halts
    during the swap window so bots don't avalanche-halt mid-cycle.
    """
    from ib_trader.logging_.alerts import log_and_alert

    interval_h = float(ctx.settings.get(
        "prophylactic_resub_interval_hours", DEFAULT_PROPHYLACTIC_INTERVAL_H,
    ))
    stagger_s = float(ctx.settings.get(
        "prophylactic_resub_stagger_seconds", DEFAULT_PROPHYLACTIC_STAGGER_S,
    ))
    startup_delay_s = float(ctx.settings.get(
        "prophylactic_resub_startup_delay_seconds",
        DEFAULT_PROPHYLACTIC_STARTUP_DELAY_S,
    ))

    if interval_h <= 0:
        logger.info(
            '{"event": "IB_PROPHYLACTIC_RESUB_DISABLED", '
            '"interval_h": %.3f}',
            interval_h,
        )
        return

    interval_s = interval_h * 3600.0
    logger.info(
        '{"event": "IB_PROPHYLACTIC_RESUB_SCHEDULED", '
        '"interval_h": %.2f, "stagger_s": %.2f, "startup_delay_s": %.0f}',
        interval_h, stagger_s, startup_delay_s,
    )

    if not hasattr(ctx.ib, "prophylactic_resubscribe_all"):
        logger.warning(
            '{"event": "IB_PROPHYLACTIC_RESUB_NO_API", '
            '"reason": "client lacks prophylactic_resubscribe_all"}',
        )
        return

    await asyncio.sleep(startup_delay_s)

    redis = getattr(ctx, "redis", None)

    while True:
        try:
            await asyncio.sleep(interval_s)

            streaming_count = len(getattr(ctx.ib, "_streaming", {}))
            bars_count = len(getattr(ctx.ib, "_realtime_bars", {}))
            logger.info(
                '{"event": "PROPHYLACTIC_RESUB_STARTED", '
                '"interval_h": %.2f, "streaming_count": %d, '
                '"bars_count": %d, "stagger_s": %.2f}',
                interval_h, streaming_count, bars_count, stagger_s,
            )

            # Raise an in-flight WARNING so bots suppress per-symbol
            # STALE_QUOTES halts for the duration of the swap window.
            try:
                await log_and_alert(
                    redis=redis,
                    trigger="IB_PROPHYLACTIC_RESUB_INFLIGHT",
                    message=(
                        f"Routine subscription refresh: cycling "
                        f"{streaming_count} quote + {bars_count} bar subs "
                        f"(stagger {stagger_s:.1f}s)."
                    ),
                    severity="WARNING",
                    dedup_key="prophylactic_resub",
                    exc_info=False,
                )
            except Exception:
                logger.exception(
                    '{"event": "IB_PROPHYLACTIC_RESUB_ALERT_FAILED"}',
                )

            started = asyncio.get_event_loop().time()
            try:
                result = await ctx.ib.prophylactic_resubscribe_all(
                    stagger_s=stagger_s,
                )
                duration_s = asyncio.get_event_loop().time() - started
                logger.info(
                    '{"event": "PROPHYLACTIC_RESUB_DONE", '
                    '"duration_s": %.2f, "mkt_ok": %d, "mkt_fail": %d, '
                    '"mkt_total": %d, "bar_ok": %d, "bar_fail": %d, '
                    '"bar_total": %d}',
                    duration_s,
                    result["mkt_ok"], result["mkt_fail"], result["mkt_total"],
                    result["bar_ok"], result["bar_fail"], result["bar_total"],
                )
            finally:
                # Always resolve the in-flight alert — even on failure
                # we don't want it latched (operator would think a
                # resub is still running forever).
                await _resolve_prophylactic_alert(redis)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('{"event": "IB_PROPHYLACTIC_RESUB_LOOP_ERROR"}')
            # Resolve any latched alert, then wait an hour before
            # re-arming so a persistent failure doesn't tight-loop.
            await _resolve_prophylactic_alert(redis)
            await asyncio.sleep(3600)


async def _resolve_prophylactic_alert(redis) -> None:
    """HDEL the IB_PROPHYLACTIC_RESUB_INFLIGHT alert from alerts:active.

    Mirrors the pattern in ``main._resolve_ib_disconnect_alert``. The
    dedup_key gives the alert a stable uuid5 id, so we can drop it by
    re-deriving the id rather than scanning the hash.
    """
    if redis is None:
        return
    try:
        import uuid as _uuid
        from ib_trader.redis.state import StateKeys
        from ib_trader.redis.streams import publish_activity

        alert_id = str(_uuid.uuid5(
            _uuid.NAMESPACE_DNS,
            "IB_PROPHYLACTIC_RESUB_INFLIGHT|prophylactic_resub",
        ))
        removed = await redis.hdel(StateKeys.alerts_active(), alert_id)
        if removed:
            await publish_activity(redis, "alerts")
            logger.info(
                '{"event": "IB_PROPHYLACTIC_RESUB_ALERT_RESOLVED"}',
            )
    except Exception:
        logger.exception(
            '{"event": "IB_PROPHYLACTIC_RESUB_ALERT_RESOLVE_FAILED"}',
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

async def _heartbeat_silence_seconds(ctx: "AppContext") -> float | None:
    """Seconds since the engine's last published quote tick, or None.

    Reads ``quote:{symbol}:latest`` is per-symbol; the cross-symbol
    "quote stream alive at all" signal is ``quotes:heartbeat`` (60 s
    TTL, updated on every IB tick). Missing key = no tick within the
    TTL → return None and let callers treat it as a long silence.
    """
    if ctx.redis is None:
        return None
    try:
        from ib_trader.redis.state import StateKeys
        raw = await ctx.redis.get(StateKeys.quotes_heartbeat())
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        ts_str = data.get("ts")
        if not ts_str:
            return None
        # Stored as either a `datetime.isoformat()` string or a value
        # ``StateStore.set`` JSON-encoded. ``datetime.fromisoformat``
        # parses both with offset info preserved.
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            # Assume PT for naive timestamps (server-local convention).
            ts = ts.replace(tzinfo=PT)
        delta = (datetime.now(ts.tzinfo) - ts).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _ticks_expected_now(now: datetime | None = None) -> bool:
    """Coarse "should ticks be flowing?" check.

    Returns False during the weekly market closure (Sat all-day, plus
    Sun before 17:00 CT when CME reopens) and during the CME daily
    settle window (16:00–17:00 CT Mon-Thu). True otherwise — even
    pre-market and ETH should still produce some tick activity on a
    typical watchlist within 60 s.
    """
    from ib_trader.engine.market_hours_futures import is_cme_equity_break

    pt_now = (now or datetime.now(PT)).astimezone(PT)
    weekday = pt_now.weekday()  # Mon=0 … Sun=6

    # Weekend closure: Saturday all day; Sunday before 15:00 PT
    # (= 17:00 CT, CME reopen).
    if weekday == 5:  # Saturday
        return False
    if weekday == 6 and pt_now.hour < 15:  # Sunday before reopen
        return False

    # CME daily settle (advisory — most equities still tick during this
    # window, but practical experience shows tick flow can pause).
    if is_cme_equity_break(pt_now):
        return False

    return True


def _capture_ib_state(ctx: "AppContext") -> dict:
    """Compact snapshot of ib_async client + ticker state for postmortem."""
    snap: dict = {}
    try:
        ib = getattr(ctx.ib, "_InsyncClient__ib", None)  # name-mangled
        if ib is not None:
            client = getattr(ib, "client", None)
            snap["connState"] = getattr(client, "connState", None) if client else None
            snap["isConnected"] = bool(ib.isConnected()) if hasattr(ib, "isConnected") else None
    except Exception:
        snap["client_introspect_error"] = True

    try:
        if hasattr(ctx.ib, "get_tickers"):
            tickers = ctx.ib.get_tickers()
            snap["ticker_count"] = len(tickers)
            rows = []
            for t in tickers[:25]:  # cap so the log line stays sane
                contract = getattr(t, "contract", None)
                rows.append({
                    "symbol": getattr(contract, "symbol", None) if contract else None,
                    "localSymbol": getattr(contract, "localSymbol", None) if contract else None,
                    "secType": getattr(contract, "secType", None) if contract else None,
                    "conId": getattr(contract, "conId", None) if contract else None,
                    "bid": _safe_float(getattr(t, "bid", None)),
                    "ask": _safe_float(getattr(t, "ask", None)),
                    "last": _safe_float(getattr(t, "last", None)),
                })
            snap["tickers"] = rows
    except Exception:
        snap["ticker_introspect_error"] = True

    return snap


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None
