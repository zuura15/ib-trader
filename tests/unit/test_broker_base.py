"""Tests for the broker abstraction layer.

Covers: BrokerClientBase interface, capabilities, types, market hours,
fill stream, factory, and legacy compatibility methods.
"""
import pytest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ib_trader.broker.types import (
    BrokerCapabilities, Instrument, Snapshot, OrderResult, FillResult,
)
from ib_trader.broker.fill_stream import FillStream
from ib_trader.broker.ib.hours import IBMarketHours
from ib_trader.broker.alpaca.hours import AlpacaMarketHours
from ib_trader.broker.factory import create_broker, BrokerConfigError

ET = ZoneInfo("America/New_York")


class TestBrokerTypes:
    """Broker type dataclasses."""

    def test_instrument_fields(self):
        i = Instrument(
            asset_id="12345", symbol="AAPL", exchange="SMART",
            currency="USD", multiplier=None, broker="ib", raw="{}"
        )
        assert i.asset_id == "12345"
        assert i.broker == "ib"

    def test_snapshot_decimal(self):
        s = Snapshot(bid=Decimal("100.00"), ask=Decimal("100.10"), last=Decimal("100.05"))
        assert s.bid == Decimal("100.00")

    def test_order_result(self):
        r = OrderResult(
            status="Filled", qty_filled=Decimal("100"),
            avg_fill_price=Decimal("150.25"), commission=Decimal("1.00"),
        )
        assert r.status == "Filled"

    def test_fill_result(self):
        f = FillResult(
            broker_order_id="abc", qty_filled=Decimal("50"),
            avg_fill_price=Decimal("200.00"), commission=Decimal("0"),
        )
        assert f.commission == Decimal("0")

    def test_capabilities_frozen(self):
        c = BrokerCapabilities(
            supports_in_place_amend=True, supports_extended_hours=True,
            supports_overnight=True, supports_fractional_shares=False,
            supports_stop_orders=True, commission_free=False,
            fill_delivery="push", rate_limit_interval_ms=100,
            max_concurrent_connections=32,
        )
        with pytest.raises(AttributeError):
            c.supports_in_place_amend = False


class TestIBMarketHours:
    """IB market hours provider."""

    def test_rth_active(self):
        # Monday 10:00 AM ET
        now = datetime(2026, 3, 16, 10, 0, 0, tzinfo=ET)
        h = IBMarketHours()
        assert h.is_session_active(now) is True

    def test_weekend_not_active(self):
        # Saturday noon ET
        now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=ET)
        h = IBMarketHours()
        assert h.is_session_active(now) is False

    def test_overnight_extended(self):
        # Monday 2:00 AM ET (overnight)
        now = datetime(2026, 3, 16, 2, 0, 0, tzinfo=ET)
        h = IBMarketHours()
        assert h.is_extended_hours(now) is True

    def test_rth_not_extended(self):
        # Monday 10:00 AM ET
        now = datetime(2026, 3, 16, 10, 0, 0, tzinfo=ET)
        h = IBMarketHours()
        assert h.is_extended_hours(now) is False

    def test_overnight_order_params(self):
        # Monday 2:00 AM ET
        now = datetime(2026, 3, 16, 2, 0, 0, tzinfo=ET)
        h = IBMarketHours()
        params = h.order_params(now)
        assert params["tif"] == "DAY"
        assert params["extended_hours"] is True

    def test_rth_order_params(self):
        # Monday 10:00 AM ET
        now = datetime(2026, 3, 16, 10, 0, 0, tzinfo=ET)
        h = IBMarketHours()
        params = h.order_params(now)
        assert params["tif"] == "GTC"
        assert params["extended_hours"] is True

    def test_always_supports_market_orders(self):
        h = IBMarketHours()
        # Even during overnight
        now = datetime(2026, 3, 16, 2, 0, 0, tzinfo=ET)
        assert h.supports_market_orders(now) is True


class TestAlpacaMarketHours:
    """Alpaca market hours provider."""

    def test_rth_active(self):
        now = datetime(2026, 3, 16, 10, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.is_session_active(now) is True

    def test_premarket_active(self):
        now = datetime(2026, 3, 16, 5, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.is_session_active(now) is True

    def test_afterhours_active(self):
        now = datetime(2026, 3, 16, 18, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.is_session_active(now) is True

    def test_overnight_not_active(self):
        # 2:00 AM ET — Alpaca has no overnight
        now = datetime(2026, 3, 16, 2, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.is_session_active(now) is False

    def test_weekend_not_active(self):
        now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.is_session_active(now) is False

    def test_premarket_is_extended(self):
        now = datetime(2026, 3, 16, 5, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.is_extended_hours(now) is True

    def test_afterhours_is_extended(self):
        now = datetime(2026, 3, 16, 18, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.is_extended_hours(now) is True

    def test_rth_not_extended(self):
        now = datetime(2026, 3, 16, 10, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.is_extended_hours(now) is False

    def test_extended_hours_order_params(self):
        now = datetime(2026, 3, 16, 5, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        params = h.order_params(now)
        assert params["tif"] == "day"
        assert params["extended_hours"] is True

    def test_rth_order_params(self):
        now = datetime(2026, 3, 16, 10, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        params = h.order_params(now)
        assert params["tif"] == "gtc"
        assert params["extended_hours"] is False

    def test_no_market_orders_during_extended(self):
        now = datetime(2026, 3, 16, 5, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.supports_market_orders(now) is False

    def test_market_orders_during_rth(self):
        now = datetime(2026, 3, 16, 10, 0, 0, tzinfo=ET)
        h = AlpacaMarketHours()
        assert h.supports_market_orders(now) is True

    def test_session_labels(self):
        h = AlpacaMarketHours()
        assert "pre-market" in h.session_label(datetime(2026, 3, 16, 5, 0, 0, tzinfo=ET))
        assert "regular" in h.session_label(datetime(2026, 3, 16, 10, 0, 0, tzinfo=ET))
        assert "after-hours" in h.session_label(datetime(2026, 3, 16, 18, 0, 0, tzinfo=ET))
        assert "weekend" in h.session_label(datetime(2026, 3, 14, 12, 0, 0, tzinfo=ET))


class TestFillStream:
    """FillStream base class."""

    def test_check_filled_returns_none_for_unknown(self):
        class DummyStream(FillStream):
            async def wait_for_fill(self, bid, timeout=30): return None
            async def start(self): pass
            async def stop(self): pass

        s = DummyStream()
        assert s.check_filled("unknown") is None

    def test_check_filled_returns_cached_result(self):
        class DummyStream(FillStream):
            async def wait_for_fill(self, bid, timeout=30): return None
            async def start(self): pass
            async def stop(self): pass

        s = DummyStream()
        s._results["order1"] = FillResult(
            broker_order_id="order1", qty_filled=Decimal("100"),
            avg_fill_price=Decimal("150"), commission=Decimal("0"),
        )
        result = s.check_filled("order1")
        assert result is not None
        assert result.qty_filled == Decimal("100")

    def test_clear_removes_result(self):
        class DummyStream(FillStream):
            async def wait_for_fill(self, bid, timeout=30): return None
            async def start(self): pass
            async def stop(self): pass

        s = DummyStream()
        s._results["order1"] = FillResult(
            broker_order_id="order1", qty_filled=Decimal("100"),
            avg_fill_price=Decimal("150"), commission=Decimal("0"),
        )
        s.clear("order1")
        assert s.check_filled("order1") is None


class TestBrokerFactory:
    """Broker factory tests."""

    def test_unknown_broker_raises(self):
        with pytest.raises(BrokerConfigError, match="Unknown broker"):
            create_broker("unknown", {})

    def test_alpaca_missing_keys_raises(self):
        with pytest.raises(BrokerConfigError, match="ALPACA_API_KEY"):
            create_broker("alpaca", {}, env_vars={})


class TestLegacyCompatibility:
    """BrokerClientBase legacy methods for backward compat with IBClientBase."""

    @pytest.mark.asyncio
    async def test_qualify_contract_delegates_to_resolve_instrument(self, mock_ib):
        """The legacy qualify_contract should still work."""
        result = await mock_ib.qualify_contract("AAPL")
        assert "con_id" in result
        assert result["con_id"] == 12345

    @pytest.mark.asyncio
    async def test_get_market_snapshot_legacy(self, mock_ib):
        """The legacy get_market_snapshot(con_id: int) should still work."""
        result = await mock_ib.get_market_snapshot(12345)
        assert "bid" in result
        assert "ask" in result
