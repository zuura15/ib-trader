"""End-to-end Ford console workflow smoke test — LIVE account.

Run with:  pytest -m smoke tests/smoke/test_console_ford_workflow.py -v

Simulates a REPL session against the real broker connection:

  1. Subscribe F to streaming market data.
  2. Verify the engine's pendingTickersEvent-driven publisher is pushing
     quotes to the Redis `quote:F` stream.
  3. buy F 1 (market/ASK)     → fills, IB position += 1
  4. buy F 1 mid              → fills (repricing), IB position += 1
  5. Read open trade groups — expect two open F trades.
  6. close <mid serial>       → closes the mid leg, IB position -= 1
  7. sell F 1 (market/BID)    → fresh SELL, net IB position returns to
                                baseline, catching any stuck long.

SESSION AWARENESS
-----------------
  - The test skips only during the weekend closure + the nightly 3:50–
    4:00 AM ET session break (no IB session active).
  - During RTH the buy/sell/close steps that the user expressed as
    "market" use Strategy.MARKET.
  - Outside RTH, Strategy.MARKET is not reliably accepted for all routes,
    so the test swaps in aggressive limits that still cross the spread:
    BUY → Strategy.ASK, SELL / close-long → Strategy.BID.
  - The "mid" buy uses Strategy.MID in every session — that's a direct
    regression check on the reprice loop.

SAFETY
------
  - Never more than 1 share per order (~$12 at current F prices).
  - Class teardown ALWAYS flattens any remaining F long or short via a
    market order (or aggressive limit outside RTH), then disconnects.
  - Every assertion that a position changed demands a non-zero delta —
    if IB rejects a fill or a reprice times out, the test fails loudly.

LIVE ACCOUNT
------------
Uses IB_PORT (default 4001) and IB_ACCOUNT_ID from .env, not the paper
port. Expected cost per full run: ~$5 in commissions + spread.

NEVER run in CI.
"""
import asyncio
import os
from decimal import Decimal
from functools import wraps

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.config.context import AppContext
from ib_trader.data.models import Base
from ib_trader.data.repository import (
    TradeRepository, RepriceEventRepository,
    ContractRepository, HeartbeatRepository, AlertRepository,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.engine.market_hours import (
    _now_et, is_ib_session_active, is_session_break, is_weekend_closure,
    session_label,
)
from ib_trader.engine.order import execute_order, execute_close
from ib_trader.engine.service import _handle_builtin
from ib_trader.engine.tracker import OrderTracker
from ib_trader.ib.insync_client import InsyncClient
from ib_trader.redis.streams import StreamNames
from ib_trader.repl.commands import BuyCommand, CloseCommand, SellCommand, Strategy

load_dotenv()

IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4001"))          # live
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID_SMOKE", "97"))
IB_ACCOUNT_ID = os.environ.get("IB_ACCOUNT_ID", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

SYMBOL = "F"
QTY = Decimal("1")

# Upper bound to wait for each fill (market + mid reprice_duration + slack).
FILL_TIMEOUT_S = 30.0
# How long we'll wait for a quote to land in the Redis stream.
QUOTE_WAIT_S = 15.0
# How long we wait for the IB position cache to reflect the latest fill.
POSITION_SETTLE_S = 10.0


def any_active_session(func):
    """Skip only when no IB session is active (weekend closure or 3:50–4:00 AM break)."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        if is_weekend_closure():
            pytest.skip("Weekend closure — no trading")
        if is_session_break():
            pytest.skip("IB session break (3:50–4:00 AM ET)")
        return await func(*args, **kwargs)
    return wrapper


def _is_rth() -> bool:
    """True during 9:30 AM – 4:00 PM ET, Mon–Fri."""
    n = _now_et()
    if n.weekday() >= 5:
        return False
    mins = n.hour * 60 + n.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def _fill_strategy(side: str, user_intent: Strategy) -> Strategy:
    """Return a strategy that should actually fill in the current session.

    The user wrote the workflow as "market" and "mid". In RTH, Strategy.MARKET
    fills instantly; outside RTH market routes are unreliable, so we swap in
    aggressive limits that still cross the spread:
        - BUY  → Strategy.ASK  (buy at the ask = crosses the spread)
        - SELL → Strategy.BID  (sell at the bid = crosses the spread)
    Strategy.MID is left as-is in every session — exercising the reprice
    loop is part of the test's intent.
    """
    if user_intent != Strategy.MARKET:
        return user_intent
    if _is_rth():
        return Strategy.MARKET
    return Strategy.ASK if side.upper() == "BUY" else Strategy.BID


async def _ib_position_qty(ib: InsyncClient, symbol: str) -> Decimal:
    """Return the net signed position for ``symbol`` on the live account."""
    ib_obj = ib._ib
    await asyncio.wait_for(ib_obj.reqPositionsAsync(), timeout=10)
    total = Decimal("0")
    for p in ib_obj.positions():
        if getattr(p.contract, "symbol", None) == symbol:
            total += Decimal(str(p.position))
    return total


async def _wait_for_position(
    ib: InsyncClient, symbol: str, expected: Decimal,
    timeout: float = POSITION_SETTLE_S,
) -> Decimal:
    """Poll IB until the net position for ``symbol`` equals ``expected``.

    IB's positionEvent lags the fill callback by a few hundred milliseconds.
    We poll (via reqPositionsAsync) at 0.5s intervals rather than sleeping
    a flat amount so the test is as fast as the broker allows.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        last = await _ib_position_qty(ib, symbol)
        if last == expected:
            return last
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"Position for {symbol} did not reach {expected} within {timeout}s "
        f"(last seen: {last})"
    )


@pytest.mark.smoke
@pytest.mark.asyncio(loop_scope="class")
class TestFordConsoleWorkflow:
    """Ordered end-to-end workflow against the live account.

    Methods are named test_01_, test_02_, ... so pytest runs them in order
    within the class (declaration order is guaranteed since pytest 3.x).
    Shared state hangs off ``cls`` — serials, baseline, callbacks — so each
    step only asserts the delta it is responsible for.
    """

    # Shared state across ordered steps (populated by the fixtures/tests).
    baseline: Decimal = Decimal("0")
    market_serial: int | None = None
    mid_serial: int | None = None
    _flatten_requested: bool = False

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def live_ctx(self):
        """Live IB + Redis + in-memory SQLite wired into an AppContext."""
        if is_weekend_closure():
            pytest.skip("Weekend closure — no trading")
        if is_session_break():
            pytest.skip(f"IB session break ({session_label()})")
        if not is_ib_session_active():
            pytest.skip(f"No active IB session ({session_label()})")
        if not IB_ACCOUNT_ID:
            pytest.skip("IB_ACCOUNT_ID not set — set it in .env for live smoke tests")

        ib = InsyncClient(
            host=IB_HOST, port=IB_PORT, client_id=IB_CLIENT_ID,
            account_id=IB_ACCOUNT_ID, min_call_interval_ms=200,
            connect_timeout=10,
        )
        try:
            await asyncio.wait_for(ib.connect(), timeout=10)
        except (Exception, asyncio.TimeoutError) as e:
            pytest.skip(f"IB Gateway not reachable at {IB_HOST}:{IB_PORT}: {e}")

        # In-memory SQLite so trade groups / transactions can be queried.
        engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        sf = scoped_session(sessionmaker(bind=engine))

        # Live Redis — if unavailable, the quote-stream step will skip but
        # the order-flow steps still run.
        redis = None
        try:
            from ib_trader.redis.client import get_redis
            redis = await get_redis(REDIS_URL)
        except Exception:
            redis = None

        settings = {
            "max_order_size_shares": 5,
            "max_retries": 3,
            "retry_delay_seconds": 2,
            "retry_backoff_multiplier": 2.0,
            "reprice_steps": 10,
            "reprice_active_duration_seconds": 10,
            "reprice_passive_wait_seconds": 20,
            "ib_host": IB_HOST, "ib_port": IB_PORT, "ib_client_id": IB_CLIENT_ID,
            "ib_min_call_interval_ms": 200,
            "cache_ttl_seconds": 86400,
            "heartbeat_interval_seconds": 30,
            "heartbeat_stale_threshold_seconds": 300,
        }
        ctx = AppContext(
            ib=ib,
            trades=TradeRepository(sf),
            reprice_events=RepriceEventRepository(sf),
            contracts=ContractRepository(sf),
            heartbeats=HeartbeatRepository(sf),
            alerts=AlertRepository(sf),
            tracker=OrderTracker(),
            settings=settings,
            account_id=IB_ACCOUNT_ID,
            transactions=TransactionRepository(sf),
            pending_commands=PendingCommandRepository(sf),
            redis=redis,
        )

        # Start the event-driven tick publisher in the background so the
        # Redis quote stream actually receives ticks for F. Without this
        # task, subscribe_market_data still populates the IB-side ticker
        # cache but nothing publishes to Redis.
        tick_task = None
        if redis is not None:
            from ib_trader.engine.main import _tick_publisher_loop
            tick_task = asyncio.create_task(_tick_publisher_loop(ctx))

        try:
            baseline = await _ib_position_qty(ib, SYMBOL)
            type(self).baseline = baseline
            yield ctx
        finally:
            # Safety net: always flatten any F exposure we accumulated,
            # whether a test passed, failed, or errored mid-flight. During
            # RTH a market order is the cleanest path; outside RTH the
            # overnight session accepts market orders with includeOvernight=True
            # via insync_client.place_market_order, but pre/post-market can
            # reject them — fall back to an aggressive limit in that case.
            try:
                net = await _ib_position_qty(ib, SYMBOL)
                drift = net - type(self).baseline
                if drift != 0:
                    side = "SELL" if drift > 0 else "BUY"
                    qty = abs(drift)
                    info = await ib.qualify_contract(SYMBOL)
                    con_id = info["con_id"]
                    try:
                        await ib.place_market_order(
                            con_id=con_id, symbol=SYMBOL,
                            side=side, qty=qty, outside_rth=True,
                        )
                    except Exception:
                        snap = await ib.get_market_snapshot(con_id)
                        # Aggressive cross: BUY at ask, SELL at bid.
                        price = snap["ask"] if side == "BUY" else snap["bid"]
                        if price and price > 0:
                            await ib.place_limit_order(
                                con_id=con_id, symbol=SYMBOL,
                                side=side, qty=qty, price=price,
                                outside_rth=True, tif="GTC",
                            )
                    await asyncio.sleep(3)  # let the broker settle
            except Exception:
                pass

            if tick_task is not None:
                tick_task.cancel()
                try:
                    await tick_task
                except (asyncio.CancelledError, Exception):
                    pass

            try:
                await ib.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Step 1: subscribe to F's streaming market data.
    # ------------------------------------------------------------------ #

    @any_active_session
    async def test_01_subscribe_market_data(self, live_ctx):
        ctx = live_ctx
        info = await asyncio.wait_for(
            ctx.ib.qualify_contract(SYMBOL), timeout=30,
        )
        await asyncio.wait_for(
            ctx.ib.subscribe_market_data(info["con_id"], SYMBOL), timeout=30,
        )
        # sub is ref-counted; record con_id for later steps that need it.
        type(self)._con_id = info["con_id"]
        assert info["con_id"] > 0

    # ------------------------------------------------------------------ #
    # Step 2: the pendingTickersEvent-driven tick publisher must populate
    # the `quote:F` Redis stream within a few seconds.
    # ------------------------------------------------------------------ #

    @any_active_session
    async def test_02_quote_stream_receives_ticks(self, live_ctx):
        ctx = live_ctx
        if ctx.redis is None:
            pytest.skip("Redis not reachable — cannot verify quote stream")

        stream = StreamNames.quote(SYMBOL)
        deadline = asyncio.get_event_loop().time() + QUOTE_WAIT_S
        length = 0
        while asyncio.get_event_loop().time() < deadline:
            length = await ctx.redis.xlen(stream)
            if length > 0:
                break
            await asyncio.sleep(0.5)

        assert length > 0, (
            f"Expected at least one entry on {stream} within {QUOTE_WAIT_S}s "
            "— the tick publisher is not publishing to Redis"
        )

        # Assert the published payload has the shape downstream consumers
        # (bots, WS quote push) rely on.
        latest = await ctx.redis.xrevrange(stream, count=1)
        assert latest, "stream went empty between xlen and xrevrange"
        _entry_id, raw = latest[0]
        import json as _json
        data = {k: _json.loads(v) for k, v in raw.items()}
        assert "bid" in data and "ask" in data and "ts" in data

    # ------------------------------------------------------------------ #
    # Step 3: buy F 1 market — immediate fill.
    # ------------------------------------------------------------------ #

    @any_active_session
    async def test_03_buy_market_increments_position(self, live_ctx):
        ctx = live_ctx
        before = await _ib_position_qty(ctx.ib, SYMBOL)

        strategy = _fill_strategy("BUY", Strategy.MARKET)
        cmd = BuyCommand(
            symbol=SYMBOL, qty=QTY, dollars=None, strategy=strategy,
            profit_amount=None, take_profit_price=None, stop_loss=None,
        )
        await asyncio.wait_for(execute_order(cmd, ctx), timeout=FILL_TIMEOUT_S)

        after = await _wait_for_position(ctx.ib, SYMBOL, before + QTY)
        assert after == before + QTY, (
            f"Expected position {before + QTY}, got {after}"
        )

        # Find the trade group serial we just created so step 6 can close
        # it if it turns out to be the "mid" — we grab the serial here and
        # refine after the mid buy.
        open_trades = [t for t in ctx.trades.get_open() if t.symbol == SYMBOL]
        assert open_trades, "expected at least one open F trade group"
        type(self).market_serial = max(t.serial_number for t in open_trades)

    # ------------------------------------------------------------------ #
    # Step 4: buy F 1 mid — reprices until fill.
    # ------------------------------------------------------------------ #

    @any_active_session
    async def test_04_buy_mid_increments_position(self, live_ctx):
        ctx = live_ctx
        before = await _ib_position_qty(ctx.ib, SYMBOL)

        cmd = BuyCommand(
            symbol=SYMBOL, qty=QTY, dollars=None, strategy=Strategy.MID,
            profit_amount=None, take_profit_price=None, stop_loss=None,
        )
        await asyncio.wait_for(execute_order(cmd, ctx), timeout=FILL_TIMEOUT_S)

        after = await _wait_for_position(ctx.ib, SYMBOL, before + QTY)
        assert after == before + QTY

        # The new serial (> the market serial) is the mid leg.
        open_trades = [t for t in ctx.trades.get_open() if t.symbol == SYMBOL]
        assert len(open_trades) >= 2, (
            f"expected ≥2 open F trades after two buys, got {len(open_trades)}"
        )
        mid_candidates = [
            t.serial_number for t in open_trades
            if t.serial_number != type(self).market_serial
        ]
        assert mid_candidates, "could not distinguish mid leg from market leg"
        type(self).mid_serial = max(mid_candidates)

    # ------------------------------------------------------------------ #
    # Step 5: `orders` builtin + open trade groups — there should be two
    # open F trade groups after the buys.
    # ------------------------------------------------------------------ #

    @any_active_session
    async def test_05_open_trades_visible(self, live_ctx):
        ctx = live_ctx
        open_trades = [t for t in ctx.trades.get_open() if t.symbol == SYMBOL]
        assert len(open_trades) >= 2, (
            f"expected ≥2 open F trade groups, got {len(open_trades)}"
        )
        serials = {t.serial_number for t in open_trades}
        assert type(self).market_serial in serials
        assert type(self).mid_serial in serials

        # The `orders` builtin reports OPEN transaction rows (pending IB
        # orders). Both buys have filled by now, so we expect the builtin
        # to report none — the serial we close in step 6 comes from the
        # trade group, not from this output.
        out = _handle_builtin("orders", ctx)
        assert isinstance(out, str)

    # ------------------------------------------------------------------ #
    # Step 6: close the mid serial with a market order.
    # ------------------------------------------------------------------ #

    @any_active_session
    async def test_06_close_mid_decrements_position(self, live_ctx):
        ctx = live_ctx
        assert type(self).mid_serial is not None, "step 4 did not set mid_serial"
        before = await _ib_position_qty(ctx.ib, SYMBOL)

        # Close of a LONG = SELL, so pick the session-appropriate strategy
        # that crosses the spread.
        close_strategy = _fill_strategy("SELL", Strategy.MARKET)
        cmd = CloseCommand(
            serial=type(self).mid_serial, strategy=close_strategy,
            profit_amount=None, take_profit_price=None,
        )
        await asyncio.wait_for(execute_close(cmd, ctx), timeout=FILL_TIMEOUT_S)

        after = await _wait_for_position(ctx.ib, SYMBOL, before - QTY)
        assert after == before - QTY

    # ------------------------------------------------------------------ #
    # Step 7: naked SELL market — brings net exposure back to baseline.
    # The test REQUIRES a long F position to still exist at this point:
    # if we've drifted to 0 already, something upstream went wrong.
    # ------------------------------------------------------------------ #

    @any_active_session
    async def test_07_sell_market_returns_to_baseline(self, live_ctx):
        ctx = live_ctx
        before = await _ib_position_qty(ctx.ib, SYMBOL)
        assert before > type(self).baseline, (
            f"expected an open F long before the sell, got {before} "
            f"(baseline {type(self).baseline})"
        )

        strategy = _fill_strategy("SELL", Strategy.MARKET)
        cmd = SellCommand(
            symbol=SYMBOL, qty=QTY, dollars=None, strategy=strategy,
            profit_amount=None, take_profit_price=None, stop_loss=None,
        )
        await asyncio.wait_for(execute_order(cmd, ctx), timeout=FILL_TIMEOUT_S)

        after = await _wait_for_position(ctx.ib, SYMBOL, before - QTY)
        assert after == before - QTY
        assert after == type(self).baseline, (
            f"expected net position to return to baseline {type(self).baseline}, "
            f"got {after}"
        )
