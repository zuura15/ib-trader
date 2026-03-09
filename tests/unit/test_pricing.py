"""Unit tests for engine/pricing.py.

All functions are pure — no IB calls, no DB access.
Tests verify exact Decimal arithmetic and edge cases.
"""
import pytest
from decimal import Decimal

from ib_trader.engine.pricing import (
    calc_mid,
    calc_step_price,
    calc_profit_taker_price,
    calc_profit_taker_price_short,
    calc_shares_from_dollars,
)


class TestCalcMid:
    def test_basic_mid(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        result = calc_mid(bid, ask)
        assert result == Decimal("100.05")

    def test_mid_rounded_to_2_places(self):
        # (100.01 + 100.03) / 2 = 100.02 — exact 2dp, no rounding needed
        bid = Decimal("100.01")
        ask = Decimal("100.03")
        result = calc_mid(bid, ask)
        assert result == Decimal("100.02")

    def test_mid_half_cent_rounds_to_even(self):
        # (100.01 + 100.02) / 2 = 100.015 → ROUND_HALF_EVEN → 100.02
        bid = Decimal("100.01")
        ask = Decimal("100.02")
        result = calc_mid(bid, ask)
        assert result == Decimal("100.02")

    def test_mid_equal_bid_ask(self):
        price = Decimal("150.25")
        assert calc_mid(price, price) == Decimal("150.25")

    def test_mid_large_spread(self):
        result = calc_mid(Decimal("50.00"), Decimal("60.00"))
        assert result == Decimal("55.00")

    def test_no_float_used(self):
        result = calc_mid(Decimal("412.20"), Decimal("412.40"))
        assert isinstance(result, Decimal)
        assert result == Decimal("412.30")


class TestCalcStepPrice:
    def test_step_0_returns_mid(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        mid = calc_mid(bid, ask)
        result = calc_step_price(bid, ask, 0, 10, "BUY")
        assert result == mid

    def test_buy_step_equals_total_returns_ask(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        result = calc_step_price(bid, ask, 10, 10, "BUY")
        assert result == ask

    def test_buy_step_1_of_10(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        # mid = 100.05, ask - mid = 0.05, step 1/10 * 0.05 = 0.005
        # price = 100.05 + 0.005 = 100.055 → ROUND_HALF_EVEN → 100.06
        result = calc_step_price(bid, ask, 1, 10, "BUY")
        assert result == Decimal("100.06")

    def test_buy_step_5_of_10(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        # mid = 100.05, step 5/10 * 0.05 = 0.025 → 100.075 → ROUND_HALF_EVEN → 100.08
        result = calc_step_price(bid, ask, 5, 10, "BUY")
        assert result == Decimal("100.08")

    def test_sell_step_equals_total_returns_bid(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        result = calc_step_price(bid, ask, 10, 10, "SELL")
        assert result == bid

    def test_sell_step_1_of_10(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        # mid = 100.05, bid - mid = -0.05, step 1/10 * -0.05 = -0.005
        # price = 100.05 - 0.005 = 100.045 → ROUND_HALF_EVEN → 100.04
        result = calc_step_price(bid, ask, 1, 10, "SELL")
        assert result == Decimal("100.04")

    def test_sell_step_5_of_10(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        # mid = 100.05, step 5/10 * -0.05 = -0.025 → 100.025 → ROUND_HALF_EVEN → 100.02
        result = calc_step_price(bid, ask, 5, 10, "SELL")
        assert result == Decimal("100.02")

    def test_sell_monotonically_decreasing(self):
        bid = Decimal("200.00")
        ask = Decimal("201.00")
        prices = [calc_step_price(bid, ask, i, 10, "SELL") for i in range(11)]
        for i in range(1, len(prices)):
            assert prices[i] <= prices[i - 1]

    def test_zero_total_steps_raises(self):
        with pytest.raises(ValueError, match="total_steps"):
            calc_step_price(Decimal("100"), Decimal("101"), 1, 0)

    def test_buy_monotonically_increasing(self):
        bid = Decimal("200.00")
        ask = Decimal("201.00")
        prices = [calc_step_price(bid, ask, i, 10, "BUY") for i in range(11)]
        for i in range(1, len(prices)):
            assert prices[i] >= prices[i - 1]

    def test_default_side_is_buy(self):
        bid = Decimal("100.00")
        ask = Decimal("100.10")
        # Default should behave identical to explicit "BUY"
        assert calc_step_price(bid, ask, 10, 10) == ask


class TestCalcProfitTakerPrice:
    def test_basic_long_profit_taker(self):
        # avg_fill=100, qty=10, profit=50 → profit/qty=5 → 105
        result = calc_profit_taker_price(Decimal("100.00"), Decimal("10"), Decimal("50"))
        assert result == Decimal("105.00")

    def test_fractional_per_share_profit(self):
        # avg_fill=412.33, qty=100, profit=500 → 412.33 + 5 = 417.33
        result = calc_profit_taker_price(Decimal("412.33"), Decimal("100"), Decimal("500"))
        assert result == Decimal("417.33")

    def test_zero_qty_raises(self):
        with pytest.raises(ValueError, match="qty_filled"):
            calc_profit_taker_price(Decimal("100"), Decimal("0"), Decimal("500"))

    def test_returns_decimal(self):
        result = calc_profit_taker_price(Decimal("100"), Decimal("10"), Decimal("100"))
        assert isinstance(result, Decimal)


class TestCalcProfitTakerPriceShort:
    def test_short_profit_taker_lower_than_entry(self):
        # Short entry at 100, profit 50, qty 10 → cover at 95
        result = calc_profit_taker_price_short(Decimal("100.00"), Decimal("10"), Decimal("50"))
        assert result == Decimal("95.00")

    def test_zero_qty_raises(self):
        with pytest.raises(ValueError, match="qty_filled"):
            calc_profit_taker_price_short(Decimal("100"), Decimal("0"), Decimal("50"))


class TestCalcSharesFromDollars:
    def test_basic_conversion(self):
        # $1000 / $100 = 10 shares
        result = calc_shares_from_dollars(Decimal("1000"), Decimal("100"), max_shares=100)
        assert result == Decimal("10")

    def test_floor_division(self):
        # $1001 / $100 = 10.01 → floor = 10
        result = calc_shares_from_dollars(Decimal("1001"), Decimal("100"), max_shares=100)
        assert result == Decimal("10")

    def test_capped_at_max_shares(self):
        # $10000 / $10 = 1000, but max is 10
        result = calc_shares_from_dollars(Decimal("10000"), Decimal("10"), max_shares=10)
        assert result == Decimal("10")

    def test_zero_price_raises(self):
        with pytest.raises(ValueError, match="price must be positive"):
            calc_shares_from_dollars(Decimal("1000"), Decimal("0"), max_shares=10)

    def test_negative_price_raises(self):
        with pytest.raises(ValueError, match="price must be positive"):
            calc_shares_from_dollars(Decimal("1000"), Decimal("-1"), max_shares=10)

    def test_very_small_amount(self):
        # $1 / $100 = 0.01 → floor = 0
        result = calc_shares_from_dollars(Decimal("1"), Decimal("100"), max_shares=10)
        assert result == Decimal("0")
