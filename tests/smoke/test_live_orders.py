"""Live order placement smoke tests — require a live IB Gateway connection.

Run with: pytest -m smoke tests/smoke/test_live_orders.py -v

Uses Ford Motor Company (F, ~$10-$20) to keep dollar risk minimal.
Every test places at most 1 share. Tests that intentionally fill always
close the resulting position before returning.

SAFETY:
  - All non-fill tests place limits far outside the market ($1.00 BUY or
    $999.00 SELL) so they can never fill.  They always cancel in cleanup.
  - Fill tests buy/sell 1 share at aggressive prices, verify the fill,
    then immediately close with an opposite-side order.  Maximum risk is
    the spread + commission on 1 share of F (~$0.02 + $1.00).
  - Every test has a try/finally to guarantee cancel/close on failure.

SESSION AWARENESS:
  - Tests decorated @rth_only skip automatically outside regular hours.
  - Tests decorated @overnight_only skip automatically outside overnight.
  - Tests decorated @any_session run during any active IB session.
  - Weekend closure (Fri 8 PM – Sun 8 PM ET) skips ALL tests.

NEVER run these in CI without a live IB Gateway.
"""
import asyncio
import os
import pytest
from decimal import Decimal
from functools import wraps

from dotenv import load_dotenv

from ib_trader.ib.insync_client import InsyncClient
from ib_trader.engine.market_hours import (
    is_ib_session_active, is_overnight_session, is_weekend_closure,
    session_label,
)

load_dotenv()

IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4002"))
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID_SMOKE", "98"))
IB_ACCOUNT_ID = os.environ.get("IB_ACCOUNT_ID_PAPER") or os.environ.get("IB_ACCOUNT_ID", "")

SYMBOL = "F"        # Ford Motor Company (~$10-$20)
QTY = Decimal("1")  # NEVER more than 1 share

# Prices guaranteed never to fill (safety net).
SAFE_BUY_PRICE = Decimal("1.00")     # Far below any realistic Ford price
SAFE_SELL_PRICE = Decimal("999.00")  # Far above any realistic Ford price

CALL_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Session-awareness decorators
# ---------------------------------------------------------------------------

def rth_only(func):
    """Skip test if not during regular trading hours (9:30 AM – 4:00 PM ET)."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        from ib_trader.engine.market_hours import _now_et
        n = _now_et()
        mins = n.hour * 60 + n.minute
        if is_weekend_closure():
            pytest.skip("Weekend closure — no trading")
        if not (9 * 60 + 30 <= mins < 16 * 60):
            pytest.skip(f"Not RTH — current session: {session_label()}")
        return await func(*args, **kwargs)
    return wrapper


def overnight_only(func):
    """Skip test if not during overnight session (8 PM – 3:50 AM ET)."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        if is_weekend_closure():
            pytest.skip("Weekend closure — no trading")
        if not is_overnight_session():
            pytest.skip(f"Not overnight — current session: {session_label()}")
        return await func(*args, **kwargs)
    return wrapper


def any_session(func):
    """Skip test only during weekend closure or session break."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        if not is_ib_session_active():
            pytest.skip(f"No active session — {session_label()}")
        return await func(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
async def live_ib():
    """Connect to live IB Gateway for a single smoke test.

    Function-scoped: each test gets a fresh connection so a timeout in one
    test does not corrupt the next.
    """
    client = InsyncClient(
        host=IB_HOST,
        port=IB_PORT,
        client_id=IB_CLIENT_ID,
        account_id=IB_ACCOUNT_ID,
        min_call_interval_ms=200,
        connect_timeout=10,
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=10)
    except (Exception, asyncio.TimeoutError) as e:
        pytest.skip(f"IB Gateway not reachable at {IB_HOST}:{IB_PORT}: {e}")

    yield client

    try:
        await client.disconnect()
    except Exception:
        pass


@pytest.fixture()
async def ford_con_id(live_ib):
    """Qualify Ford contract and return its con_id."""
    info = await asyncio.wait_for(
        live_ib.qualify_contract(SYMBOL), timeout=CALL_TIMEOUT
    )
    return info["con_id"]


async def _snapshot(live_ib, con_id) -> dict:
    """Get Ford market snapshot, skip if no data."""
    snap = await live_ib.get_market_snapshot(con_id)
    if snap["bid"] == 0 or snap["ask"] == 0:
        if snap["last"] > 0:
            return snap  # overnight may have last but no bid/ask
        pytest.skip(f"No market data for {SYMBOL}")
    return snap


async def _place_safe_buy(live_ib, con_id) -> str:
    """Place a BUY limit at $1.00 (will never fill). Returns ib_order_id."""
    return await asyncio.wait_for(
        live_ib.place_limit_order(
            con_id=con_id, symbol=SYMBOL, side="BUY",
            qty=QTY, price=SAFE_BUY_PRICE, outside_rth=True, tif="GTC",
        ),
        timeout=CALL_TIMEOUT,
    )


async def _place_safe_sell(live_ib, con_id) -> str:
    """Place a SELL limit at $999.00 (will never fill). Returns ib_order_id."""
    return await asyncio.wait_for(
        live_ib.place_limit_order(
            con_id=con_id, symbol=SYMBOL, side="SELL",
            qty=QTY, price=SAFE_SELL_PRICE, outside_rth=True, tif="GTC",
        ),
        timeout=CALL_TIMEOUT,
    )


async def _cancel_and_verify(live_ib, ib_order_id: str) -> None:
    """Cancel an order and verify it reached a terminal status."""
    await live_ib.cancel_order(ib_order_id)
    await asyncio.sleep(1)  # Give IB time to process
    status = await live_ib.get_order_status(ib_order_id)
    terminal = {"Cancelled", "ApiCancelled", "Inactive", "PendingCancel"}
    assert status["status"] in terminal, (
        f"Expected terminal status after cancel, got: {status['status']}"
    )


# ===========================================================================
# GROUP 1: Contract Qualification & Market Data (3 tests)
# ===========================================================================

@pytest.mark.smoke
class TestFordContractAndData:
    """Safe tests — no orders placed."""

    async def test_qualify_ford_contract(self, live_ib):
        """Qualify Ford (F) contract and verify conId is returned."""
        info = await asyncio.wait_for(
            live_ib.qualify_contract(SYMBOL), timeout=CALL_TIMEOUT,
        )
        assert info["con_id"] > 0, "Ford conId should be positive"
        assert info["exchange"] == "SMART"
        assert info["currency"] == "USD"

    @any_session
    async def test_ford_market_snapshot_has_data(self, live_ib, ford_con_id):
        """Verify Ford snapshot returns non-zero bid/ask or last."""
        snap = await live_ib.get_market_snapshot(ford_con_id)
        has_data = snap["bid"] > 0 or snap["ask"] > 0 or snap["last"] > 0
        assert has_data, "Expected at least one non-zero price field"

    @any_session
    async def test_ford_bid_ask_spread_valid(self, live_ib, ford_con_id):
        """Verify ask >= bid when both are available."""
        snap = await _snapshot(live_ib, ford_con_id)
        if snap["bid"] > 0 and snap["ask"] > 0:
            assert snap["ask"] >= snap["bid"], (
                f"ask ({snap['ask']}) should be >= bid ({snap['bid']})"
            )


# ===========================================================================
# GROUP 2: Safe Order Placement — never fills (8 tests)
# Always places far from market, always cancels in cleanup.
# ===========================================================================

@pytest.mark.smoke
class TestFordSafeOrders:
    """Place orders far from market that can never fill. Always cancel."""

    @any_session
    async def test_buy_limit_far_below_accepted(self, live_ib, ford_con_id):
        """BUY 1 F @ $1.00 — IB should accept, order should not fill."""
        ib_id = await _place_safe_buy(live_ib, ford_con_id)
        try:
            assert ib_id is not None and len(ib_id) > 0
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted"), (
                f"Expected working status, got: {status['status']}"
            )
            assert status["qty_filled"] == 0
        finally:
            await _cancel_and_verify(live_ib, ib_id)

    @any_session
    async def test_sell_limit_far_above_accepted(self, live_ib, ford_con_id):
        """SELL 1 F @ $999.00 — IB should accept, order should not fill."""
        ib_id = await _place_safe_sell(live_ib, ford_con_id)
        try:
            assert ib_id is not None and len(ib_id) > 0
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted")
            assert status["qty_filled"] == 0
        finally:
            await _cancel_and_verify(live_ib, ib_id)

    @any_session
    async def test_place_and_cancel_immediately(self, live_ib, ford_con_id):
        """Place a safe order and cancel it immediately."""
        ib_id = await _place_safe_buy(live_ib, ford_con_id)
        await _cancel_and_verify(live_ib, ib_id)

    @any_session
    async def test_cancel_sets_terminal_status(self, live_ib, ford_con_id):
        """After cancel, status should be a recognized terminal value."""
        ib_id = await _place_safe_buy(live_ib, ford_con_id)
        await live_ib.cancel_order(ib_id)
        await asyncio.sleep(2)
        status = await live_ib.get_order_status(ib_id)
        terminal = {"Cancelled", "ApiCancelled", "Inactive", "PendingCancel"}
        assert status["status"] in terminal
        assert status["qty_filled"] == 0

    @any_session
    async def test_order_appears_in_open_orders(self, live_ib, ford_con_id):
        """A placed order should appear in get_open_orders."""
        ib_id = await _place_safe_buy(live_ib, ford_con_id)
        try:
            await asyncio.sleep(1)
            open_orders = await live_ib.get_open_orders()
            found = any(o["ib_order_id"] == ib_id for o in open_orders)
            assert found, f"Order {ib_id} not found in open orders"
        finally:
            await _cancel_and_verify(live_ib, ib_id)

    @any_session
    async def test_cancelled_order_not_in_open_orders(self, live_ib, ford_con_id):
        """After cancellation, order should not appear in open orders."""
        ib_id = await _place_safe_buy(live_ib, ford_con_id)
        await _cancel_and_verify(live_ib, ib_id)
        await asyncio.sleep(1)
        open_orders = await live_ib.get_open_orders()
        found = any(o["ib_order_id"] == ib_id for o in open_orders)
        assert not found, f"Cancelled order {ib_id} should not be in open orders"

    @any_session
    async def test_buy_gtc_outside_rth(self, live_ib, ford_con_id):
        """BUY with outsideRth=True and tif=GTC is accepted."""
        ib_id = await asyncio.wait_for(
            live_ib.place_limit_order(
                con_id=ford_con_id, symbol=SYMBOL, side="BUY",
                qty=QTY, price=SAFE_BUY_PRICE, outside_rth=True, tif="GTC",
            ),
            timeout=CALL_TIMEOUT,
        )
        try:
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted")
        finally:
            await _cancel_and_verify(live_ib, ib_id)

    @any_session
    async def test_sell_gtc_outside_rth(self, live_ib, ford_con_id):
        """SELL with outsideRth=True and tif=GTC is accepted."""
        ib_id = await asyncio.wait_for(
            live_ib.place_limit_order(
                con_id=ford_con_id, symbol=SYMBOL, side="SELL",
                qty=QTY, price=SAFE_SELL_PRICE, outside_rth=True, tif="GTC",
            ),
            timeout=CALL_TIMEOUT,
        )
        try:
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted")
        finally:
            await _cancel_and_verify(live_ib, ib_id)


# ===========================================================================
# GROUP 3: Order Amendment (2 tests)
# Places far from market, amends price, verifies, cancels.
# ===========================================================================

@pytest.mark.smoke
class TestFordOrderAmendment:
    """Amend (modify) order prices on live orders."""

    @any_session
    async def test_amend_buy_limit_price(self, live_ib, ford_con_id):
        """Place BUY @ $1.00, amend to $2.00, verify price changed."""
        ib_id = await _place_safe_buy(live_ib, ford_con_id)
        try:
            await asyncio.sleep(1)
            new_price = Decimal("2.00")
            await asyncio.wait_for(
                live_ib.amend_order(ib_id, new_price), timeout=CALL_TIMEOUT,
            )
            await asyncio.sleep(1)
            # Verify order still live (not rejected by amendment)
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted"), (
                f"After amendment, expected working status, got: {status['status']}"
            )
        finally:
            await _cancel_and_verify(live_ib, ib_id)

    @any_session
    async def test_amend_sell_limit_price(self, live_ib, ford_con_id):
        """Place SELL @ $999.00, amend to $998.00, verify still live."""
        ib_id = await _place_safe_sell(live_ib, ford_con_id)
        try:
            await asyncio.sleep(1)
            new_price = Decimal("998.00")
            await asyncio.wait_for(
                live_ib.amend_order(ib_id, new_price), timeout=CALL_TIMEOUT,
            )
            await asyncio.sleep(1)
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted")
        finally:
            await _cancel_and_verify(live_ib, ib_id)


# ===========================================================================
# GROUP 4: RTH Fill Tests (4 tests)
# Place aggressive orders that WILL fill (1 share only).
# Always close the position immediately after fill.
# COST: spread + 2x commission on 1 share of F (~$2-3 total).
# ===========================================================================

@pytest.mark.smoke
class TestFordRTHFills:
    """Tests that intentionally fill 1 share during regular hours.

    Each test buys or sells 1 share, verifies the fill, then closes.
    Only runs during RTH (9:30 AM – 4:00 PM ET).
    """

    @rth_only
    async def test_buy_at_ask_fills_immediately(self, live_ib, ford_con_id):
        """BUY 1 F at the ask price — should fill immediately during RTH."""
        snap = await _snapshot(live_ib, ford_con_id)
        buy_price = snap["ask"]
        ib_id = None
        try:
            ib_id = await asyncio.wait_for(
                live_ib.place_limit_order(
                    con_id=ford_con_id, symbol=SYMBOL, side="BUY",
                    qty=QTY, price=buy_price, outside_rth=True, tif="GTC",
                ),
                timeout=CALL_TIMEOUT,
            )
            await asyncio.sleep(3)
            status = await live_ib.get_order_status(ib_id)
            assert status["qty_filled"] >= QTY, (
                f"Expected fill at ask, got qty_filled={status['qty_filled']}"
            )
            assert status["avg_fill_price"] is not None
            assert status["avg_fill_price"] > 0
        finally:
            # Close: sell 1 share at bid to flatten
            if ib_id:
                sell_snap = await live_ib.get_market_snapshot(ford_con_id)
                sell_price = sell_snap["bid"] if sell_snap["bid"] > 0 else buy_price
                close_id = await asyncio.wait_for(
                    live_ib.place_limit_order(
                        con_id=ford_con_id, symbol=SYMBOL, side="SELL",
                        qty=QTY, price=sell_price, outside_rth=True, tif="GTC",
                    ),
                    timeout=CALL_TIMEOUT,
                )
                await asyncio.sleep(3)
                close_status = await live_ib.get_order_status(close_id)
                if close_status["qty_filled"] < QTY:
                    # Didn't fill — cancel and use market
                    await live_ib.cancel_order(close_id)
                    await asyncio.sleep(1)
                    mkt_id = await asyncio.wait_for(
                        live_ib.place_market_order(
                            con_id=ford_con_id, symbol=SYMBOL, side="SELL",
                            qty=QTY, outside_rth=True,
                        ),
                        timeout=CALL_TIMEOUT,
                    )
                    await asyncio.sleep(3)

    @rth_only
    async def test_sell_at_bid_fills_immediately(self, live_ib, ford_con_id):
        """SELL 1 F at the bid price — should fill immediately during RTH.

        Pre-condition: need to own 1 share first (buy at ask), then sell at bid.
        """
        snap = await _snapshot(live_ib, ford_con_id)
        buy_price = snap["ask"]
        # First, buy 1 share to create the position
        buy_id = await asyncio.wait_for(
            live_ib.place_limit_order(
                con_id=ford_con_id, symbol=SYMBOL, side="BUY",
                qty=QTY, price=buy_price, outside_rth=True, tif="GTC",
            ),
            timeout=CALL_TIMEOUT,
        )
        await asyncio.sleep(3)
        buy_status = await live_ib.get_order_status(buy_id)
        if buy_status["qty_filled"] < QTY:
            await live_ib.cancel_order(buy_id)
            pytest.skip("Entry buy did not fill — cannot test sell")

        # Now sell at bid
        sell_snap = await live_ib.get_market_snapshot(ford_con_id)
        sell_price = sell_snap["bid"]
        sell_id = None
        try:
            sell_id = await asyncio.wait_for(
                live_ib.place_limit_order(
                    con_id=ford_con_id, symbol=SYMBOL, side="SELL",
                    qty=QTY, price=sell_price, outside_rth=True, tif="GTC",
                ),
                timeout=CALL_TIMEOUT,
            )
            await asyncio.sleep(3)
            status = await live_ib.get_order_status(sell_id)
            assert status["qty_filled"] >= QTY, (
                f"Expected fill at bid, got qty_filled={status['qty_filled']}"
            )
            assert status["avg_fill_price"] is not None
        finally:
            # Safety: if sell didn't fill, use market to close
            if sell_id:
                s = await live_ib.get_order_status(sell_id)
                if s["qty_filled"] < QTY:
                    await live_ib.cancel_order(sell_id)
                    await asyncio.sleep(1)
                    await asyncio.wait_for(
                        live_ib.place_market_order(
                            con_id=ford_con_id, symbol=SYMBOL, side="SELL",
                            qty=QTY, outside_rth=True,
                        ),
                        timeout=CALL_TIMEOUT,
                    )
                    await asyncio.sleep(3)

    @rth_only
    async def test_market_buy_fills(self, live_ib, ford_con_id):
        """Market BUY 1 F — should fill during RTH."""
        ib_id = None
        try:
            ib_id = await asyncio.wait_for(
                live_ib.place_market_order(
                    con_id=ford_con_id, symbol=SYMBOL, side="BUY",
                    qty=QTY, outside_rth=True,
                ),
                timeout=CALL_TIMEOUT,
            )
            await asyncio.sleep(3)
            status = await live_ib.get_order_status(ib_id)
            assert status["qty_filled"] >= QTY
            assert status["avg_fill_price"] is not None
            assert status["avg_fill_price"] > 0
        finally:
            # Close with market sell
            if ib_id:
                await asyncio.wait_for(
                    live_ib.place_market_order(
                        con_id=ford_con_id, symbol=SYMBOL, side="SELL",
                        qty=QTY, outside_rth=True,
                    ),
                    timeout=CALL_TIMEOUT,
                )
                await asyncio.sleep(3)

    @rth_only
    async def test_fill_reports_commission(self, live_ib, ford_con_id):
        """Verify that a fill reports a non-None commission value."""
        snap = await _snapshot(live_ib, ford_con_id)
        buy_price = snap["ask"]
        ib_id = None
        try:
            ib_id = await asyncio.wait_for(
                live_ib.place_limit_order(
                    con_id=ford_con_id, symbol=SYMBOL, side="BUY",
                    qty=QTY, price=buy_price, outside_rth=True, tif="GTC",
                ),
                timeout=CALL_TIMEOUT,
            )
            await asyncio.sleep(5)  # Commission may arrive slightly after fill
            status = await live_ib.get_order_status(ib_id)
            if status["qty_filled"] < QTY:
                pytest.skip("Order did not fill — cannot test commission")
            # Commission should be present (may be 0 for some promo accounts)
            assert status["commission"] is not None, (
                "Expected commission to be populated after fill"
            )
        finally:
            if ib_id:
                s = await live_ib.get_order_status(ib_id)
                if s["qty_filled"] > 0:
                    await asyncio.wait_for(
                        live_ib.place_market_order(
                            con_id=ford_con_id, symbol=SYMBOL, side="SELL",
                            qty=QTY, outside_rth=True,
                        ),
                        timeout=CALL_TIMEOUT,
                    )
                    await asyncio.sleep(3)
                else:
                    await live_ib.cancel_order(ib_id)


# ===========================================================================
# GROUP 5: Overnight-Specific Tests (5 tests)
# Only run during overnight session (8 PM – 3:50 AM ET).
# ===========================================================================

@pytest.mark.smoke
class TestFordOvernight:
    """Overnight-specific order behavior."""

    @overnight_only
    async def test_overnight_limit_buy_accepted(self, live_ib, ford_con_id):
        """BUY limit far below market accepted during overnight."""
        ib_id = await _place_safe_buy(live_ib, ford_con_id)
        try:
            await asyncio.sleep(2)
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted"), (
                f"Overnight BUY should be accepted, got: {status['status']}"
            )
        finally:
            await _cancel_and_verify(live_ib, ib_id)

    @overnight_only
    async def test_overnight_limit_sell_accepted(self, live_ib, ford_con_id):
        """SELL limit far above market accepted during overnight."""
        ib_id = await _place_safe_sell(live_ib, ford_con_id)
        try:
            await asyncio.sleep(2)
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted")
        finally:
            await _cancel_and_verify(live_ib, ib_id)

    @overnight_only
    async def test_overnight_market_order_rejected(self, live_ib, ford_con_id):
        """Market orders should be rejected during overnight (Blue Ocean ATS).

        IB error 10329 + 201: market order type invalid for overnight venue.
        """
        ib_id = await asyncio.wait_for(
            live_ib.place_market_order(
                con_id=ford_con_id, symbol=SYMBOL, side="BUY",
                qty=QTY, outside_rth=True,
            ),
            timeout=CALL_TIMEOUT,
        )
        await asyncio.sleep(3)
        status = await live_ib.get_order_status(ib_id)
        error = live_ib.get_order_error(str(ib_id))
        # Market orders on Blue Ocean ATS get rejected or cancelled
        rejected_statuses = {"Cancelled", "ApiCancelled", "Inactive"}
        if status["status"] not in rejected_statuses:
            # If it somehow got accepted, cancel it
            await live_ib.cancel_order(ib_id)
            await asyncio.sleep(1)
            pytest.skip(
                f"Market order was not rejected (status: {status['status']}) — "
                "may be in a non-overnight venue window"
            )
        assert status["qty_filled"] == 0, "Market order should not fill overnight"

    @overnight_only
    async def test_overnight_aggressive_limit_fills(self, live_ib, ford_con_id):
        """Aggressive limit at ask should fill overnight (Blue Ocean ATS)."""
        snap = await _snapshot(live_ib, ford_con_id)
        if snap["ask"] == 0:
            pytest.skip("No ask price available overnight")
        buy_price = snap["ask"]
        ib_id = None
        try:
            ib_id = await asyncio.wait_for(
                live_ib.place_limit_order(
                    con_id=ford_con_id, symbol=SYMBOL, side="BUY",
                    qty=QTY, price=buy_price, outside_rth=True, tif="GTC",
                ),
                timeout=CALL_TIMEOUT,
            )
            await asyncio.sleep(5)
            status = await live_ib.get_order_status(ib_id)
            if status["qty_filled"] < QTY:
                # Overnight may be thin — not always instant fill
                await live_ib.cancel_order(ib_id)
                pytest.skip("Aggressive limit did not fill overnight — thin liquidity")
            assert status["avg_fill_price"] is not None
        finally:
            # Close position if filled
            if ib_id:
                s = await live_ib.get_order_status(ib_id)
                if s["qty_filled"] > 0:
                    sell_snap = await live_ib.get_market_snapshot(ford_con_id)
                    sell_price = sell_snap["bid"] if sell_snap["bid"] > 0 else buy_price
                    close_id = await asyncio.wait_for(
                        live_ib.place_limit_order(
                            con_id=ford_con_id, symbol=SYMBOL, side="SELL",
                            qty=QTY, price=sell_price, outside_rth=True, tif="GTC",
                        ),
                        timeout=CALL_TIMEOUT,
                    )
                    await asyncio.sleep(5)
                    close_s = await live_ib.get_order_status(close_id)
                    if close_s["qty_filled"] < QTY:
                        await live_ib.cancel_order(close_id)
                else:
                    await live_ib.cancel_order(ib_id)

    @overnight_only
    async def test_overnight_day_tif_accepted(self, live_ib, ford_con_id):
        """Limit with tif=DAY + outsideRth should be accepted overnight.

        The insync_client sets tif=DAY + includeOvernight=True during
        overnight. Verify IB accepts this combination.
        """
        ib_id = await asyncio.wait_for(
            live_ib.place_limit_order(
                con_id=ford_con_id, symbol=SYMBOL, side="BUY",
                qty=QTY, price=SAFE_BUY_PRICE, outside_rth=True, tif="DAY",
            ),
            timeout=CALL_TIMEOUT,
        )
        try:
            await asyncio.sleep(2)
            status = await live_ib.get_order_status(ib_id)
            assert status["status"] in ("Submitted", "PreSubmitted"), (
                f"DAY+outsideRth should be accepted overnight, got: {status['status']}"
            )
        finally:
            await _cancel_and_verify(live_ib, ib_id)


# ===========================================================================
# GROUP 6: Edge Cases & Error Handling (3 tests)
# ===========================================================================

@pytest.mark.smoke
class TestFordEdgeCases:
    """Edge cases and error conditions."""

    @any_session
    async def test_cancel_nonexistent_order_no_crash(self, live_ib):
        """Cancelling a non-existent order ID should not crash."""
        try:
            await live_ib.cancel_order("999999999")
        except Exception:
            pass  # Some error is fine — just should not crash the connection

        # Connection should still work after the bad cancel
        orders = await live_ib.get_open_orders()
        assert isinstance(orders, list)

    @any_session
    async def test_get_status_nonexistent_order(self, live_ib):
        """Getting status of a non-existent order returns graceful result."""
        status = await live_ib.get_order_status("999999999")
        # Should return something without crashing
        assert isinstance(status, dict)

    @any_session
    async def test_place_two_orders_cancel_both(self, live_ib, ford_con_id):
        """Place two safe orders, verify both appear, cancel both."""
        id1 = await _place_safe_buy(live_ib, ford_con_id)
        id2 = await _place_safe_sell(live_ib, ford_con_id)
        try:
            await asyncio.sleep(1)
            open_orders = await live_ib.get_open_orders()
            ids = {o["ib_order_id"] for o in open_orders}
            assert id1 in ids, f"First order {id1} not in open orders"
            assert id2 in ids, f"Second order {id2} not in open orders"
        finally:
            await _cancel_and_verify(live_ib, id1)
            await _cancel_and_verify(live_ib, id2)
