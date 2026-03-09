"""Smoke tests — require a live IB Gateway or TWS connection.

Run with: pytest -m smoke
Skip automatically if IB Gateway is unreachable.
All tests clean up after themselves — no open orders left in IB.

NEVER run these in CI without a live IB Gateway.

Paper trading accounts (port 4002) do not have market data subscriptions by
default.  Tests that require live bid/ask quotes will skip automatically when
IB returns zero values for a contract.  TestConnectivity is the only test
guaranteed to pass on all account types.

Fixture design note
-------------------
live_ib is function-scoped (one connection per test).  This is intentional:
asyncio.wait_for cancels the underlying coroutine on timeout, which can
corrupt ib_insync internal connection state.  With a module-scoped fixture that
corruption would cascade — every subsequent test would hang too.  A fresh
connection per test means a timeout in one test has no effect on the next.
"""
import asyncio
import os
import pytest
from decimal import Decimal
from dotenv import load_dotenv

from ib_trader.ib.insync_client import InsyncClient

load_dotenv()

IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4002"))
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "99"))  # Use unique client ID for smoke
IB_ACCOUNT_ID = os.environ.get("IB_ACCOUNT_ID_PAPER") or os.environ.get("IB_ACCOUNT_ID", "")

SAFE_PRICE_OFFSET = Decimal("50.00")  # Place limit far outside market (will not fill)
FALLBACK_SAFE_PRICE = Decimal("1.00")  # Used when market data is unavailable

# Generous timeout for paper accounts which can be slower than live.
# asyncio.wait_for with this timeout is only safe because live_ib is
# function-scoped: a corrupted connection is discarded after each test.
CALL_TIMEOUT = 30  # seconds


@pytest.fixture()
async def live_ib():
    """Connect to live IB Gateway for a single smoke test.

    Function-scoped so each test gets a fresh connection.  Skips the test if
    the connection cannot be established within 10 seconds.
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

    # disconnect() calls self._ib.disconnect() which is synchronous and safe
    # even on a connection that was cancelled mid-operation.
    try:
        await client.disconnect()
    except Exception:
        pass


async def _call(coro, label: str):
    """Run an ib_insync coroutine with a hard timeout.

    On timeout the coroutine is cancelled (which may corrupt this connection's
    state), and the test is skipped.  Because live_ib is function-scoped the
    corrupted connection is discarded; the next test gets a fresh one.
    """
    try:
        return await asyncio.wait_for(coro, timeout=CALL_TIMEOUT)
    except asyncio.TimeoutError:
        pytest.skip(f"{label} did not respond within {CALL_TIMEOUT}s")


def _skip_if_no_market_data(snapshot: dict, symbol: str) -> None:
    """Skip the test when the snapshot contains no data.

    Returns zeros when the account has no market data subscription (IB error
    354).  This is an account configuration issue, not a test failure.
    """
    if snapshot["bid"] == 0 or snapshot["ask"] == 0:
        pytest.skip(
            f"No market data for {symbol} — account may not have a market data subscription"
        )


@pytest.mark.smoke
class TestConnectivity:
    async def test_connection_and_open_orders(self, live_ib):
        """Verify IB connection is live and the open-orders endpoint responds.

        Does not require a market data subscription — reliable on all account
        types including paper accounts with no data subscriptions.
        """
        orders = await _call(live_ib.get_open_orders(), "get_open_orders")
        assert isinstance(orders, list)
        for o in orders:
            assert "ib_order_id" in o
            assert "symbol" in o
            assert "status" in o


@pytest.mark.smoke
class TestLiveQuotes:
    async def test_fetch_quote_msft(self, live_ib):
        """Fetch live bid/ask for MSFT — verify non-zero values returned."""
        info = await _call(live_ib.qualify_contract("MSFT"), "qualify_contract(MSFT)")
        snapshot = await live_ib.get_market_snapshot(info["con_id"])
        _skip_if_no_market_data(snapshot, "MSFT")
        assert snapshot["bid"] > 0, "MSFT bid should be positive"
        assert snapshot["ask"] > 0, "MSFT ask should be positive"
        assert snapshot["ask"] >= snapshot["bid"], "ask should be >= bid"

    async def test_fetch_quote_aapl(self, live_ib):
        """Fetch live bid/ask for AAPL."""
        info = await _call(live_ib.qualify_contract("AAPL"), "qualify_contract(AAPL)")
        snapshot = await live_ib.get_market_snapshot(info["con_id"])
        _skip_if_no_market_data(snapshot, "AAPL")
        assert snapshot["bid"] > 0
        assert snapshot["ask"] > 0


@pytest.mark.smoke
class TestContractQualification:
    async def test_qualify_msft_returns_con_id(self, live_ib):
        """Qualify MSFT contract — verify conId returned."""
        info = await _call(live_ib.qualify_contract("MSFT"), "qualify_contract(MSFT)")
        assert info["con_id"] > 0
        assert info["exchange"] == "SMART"
        assert info["currency"] == "USD"

    async def test_qualify_aapl_returns_con_id(self, live_ib):
        """Qualify AAPL contract."""
        info = await _call(live_ib.qualify_contract("AAPL"), "qualify_contract(AAPL)")
        assert info["con_id"] > 0


@pytest.mark.smoke
class TestSafeOrderPlacement:
    async def test_place_and_cancel_limit_order(self, live_ib):
        """Place a 1-share limit far outside market, verify IB accepts, then cancel.

        Falls back to $1.00 when market data is unavailable (no subscription),
        which is safely below any real equity price.
        """
        info = await _call(live_ib.qualify_contract("MSFT"), "qualify_contract(MSFT)")
        snapshot = await live_ib.get_market_snapshot(info["con_id"])

        if snapshot["bid"] > 0:
            safe_price = snapshot["bid"] - SAFE_PRICE_OFFSET
            if safe_price <= 0:
                safe_price = FALLBACK_SAFE_PRICE
        else:
            safe_price = FALLBACK_SAFE_PRICE

        ib_order_id = await _call(
            live_ib.place_limit_order(
                con_id=info["con_id"],
                symbol="MSFT",
                side="BUY",
                qty=Decimal("1"),
                price=safe_price,
                outside_rth=True,
                tif="GTC",
            ),
            "place_limit_order",
        )

        assert ib_order_id is not None
        assert len(ib_order_id) > 0

        await _call(live_ib.cancel_order(ib_order_id), "cancel_order")

        status = await live_ib.get_order_status(ib_order_id)
        assert status["status"] in ("Cancelled", "ApiCancelled", "Inactive", "PendingCancel"), (
            f"Expected canceled status, got: {status['status']}"
        )


@pytest.mark.smoke
class TestReconciliation:
    async def test_get_open_orders_returns_list(self, live_ib):
        """Verify get_open_orders returns a valid list (may be empty)."""
        orders = await _call(live_ib.get_open_orders(), "get_open_orders")
        assert isinstance(orders, list)
        for o in orders:
            assert "ib_order_id" in o
            assert "symbol" in o
            assert "status" in o
