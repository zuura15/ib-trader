"""Unit tests for engine/pricing.py.

All functions are pure — no IB calls, no DB access.
Tests verify exact Decimal arithmetic and edge cases, across STK
(tick=0.01) and FUT (tick=0.25, multiplier=50) call sites.
"""
import pytest
from decimal import Decimal

from ib_trader.engine.pricing import (
    calc_mid,
    calc_step_price,
    calc_profit_taker_price,
    calc_profit_taker_price_short,
    calc_shares_from_dollars,
    notional_value,
)


STK = Decimal("0.01")
ES_TICK = Decimal("0.25")
ES_MULT = Decimal("50")


class TestCalcMidStk:
    def test_basic_mid(self):
        assert calc_mid(Decimal("100.00"), Decimal("100.10"), tick_size=STK) == Decimal("100.05")

    def test_mid_rounded_to_2_places(self):
        assert calc_mid(Decimal("100.01"), Decimal("100.03"), tick_size=STK) == Decimal("100.02")

    def test_mid_half_cent_banker_rounds_to_even(self):
        # (100.01 + 100.02) / 2 = 100.015 → HALF_EVEN → 100.02
        assert calc_mid(Decimal("100.01"), Decimal("100.02"), tick_size=STK) == Decimal("100.02")

    def test_mid_equal_bid_ask(self):
        price = Decimal("150.25")
        assert calc_mid(price, price, tick_size=STK) == price

    def test_mid_large_spread(self):
        assert calc_mid(Decimal("50.00"), Decimal("60.00"), tick_size=STK) == Decimal("55.00")

    def test_returns_decimal(self):
        result = calc_mid(Decimal("412.20"), Decimal("412.40"), tick_size=STK)
        assert isinstance(result, Decimal)
        assert result == Decimal("412.30")


class TestCalcMidFut:
    def test_es_mid_snaps_to_quarter(self):
        # (4500.00 + 4500.50)/2 = 4500.25 — already on tick
        assert calc_mid(Decimal("4500.00"), Decimal("4500.50"), tick_size=ES_TICK) == Decimal("4500.25")

    def test_es_mid_off_tick_snaps_nearest(self):
        # (4500.00 + 4500.10)/2 = 4500.05 → snap to 4500.00
        assert calc_mid(Decimal("4500.00"), Decimal("4500.10"), tick_size=ES_TICK) == Decimal("4500.00")


class TestCalcStepPriceStk:
    def test_step_0_returns_mid(self):
        bid, ask = Decimal("100.00"), Decimal("100.10")
        assert calc_step_price(bid, ask, 0, 10, "BUY", tick_size=STK) == calc_mid(bid, ask, tick_size=STK)

    def test_buy_step_equals_total_returns_ask(self):
        bid, ask = Decimal("100.00"), Decimal("100.10")
        assert calc_step_price(bid, ask, 10, 10, "BUY", tick_size=STK) == ask

    def test_buy_step_1_of_10(self):
        # mid=100.05, +0.005 → 100.055 → HALF_EVEN → 100.06
        assert calc_step_price(Decimal("100.00"), Decimal("100.10"), 1, 10, "BUY", tick_size=STK) == Decimal("100.06")

    def test_buy_step_5_of_10(self):
        # mid=100.05, +0.025 → 100.075 → HALF_EVEN → 100.08
        assert calc_step_price(Decimal("100.00"), Decimal("100.10"), 5, 10, "BUY", tick_size=STK) == Decimal("100.08")

    def test_sell_step_equals_total_returns_bid(self):
        bid, ask = Decimal("100.00"), Decimal("100.10")
        assert calc_step_price(bid, ask, 10, 10, "SELL", tick_size=STK) == bid

    def test_sell_step_1_of_10(self):
        # mid=100.05, -0.005 → 100.045 → HALF_EVEN → 100.04
        assert calc_step_price(Decimal("100.00"), Decimal("100.10"), 1, 10, "SELL", tick_size=STK) == Decimal("100.04")

    def test_sell_step_5_of_10(self):
        # mid=100.05, -0.025 → 100.025 → HALF_EVEN → 100.02
        assert calc_step_price(Decimal("100.00"), Decimal("100.10"), 5, 10, "SELL", tick_size=STK) == Decimal("100.02")

    def test_buy_monotonically_increasing(self):
        bid, ask = Decimal("200.00"), Decimal("201.00")
        prices = [calc_step_price(bid, ask, i, 10, "BUY", tick_size=STK) for i in range(11)]
        for i in range(1, len(prices)):
            assert prices[i] >= prices[i - 1]

    def test_sell_monotonically_decreasing(self):
        bid, ask = Decimal("200.00"), Decimal("201.00")
        prices = [calc_step_price(bid, ask, i, 10, "SELL", tick_size=STK) for i in range(11)]
        for i in range(1, len(prices)):
            assert prices[i] <= prices[i - 1]

    def test_default_side_is_buy(self):
        bid, ask = Decimal("100.00"), Decimal("100.10")
        assert calc_step_price(bid, ask, 10, 10, tick_size=STK) == ask

    def test_zero_total_steps_raises(self):
        with pytest.raises(ValueError, match="total_steps"):
            calc_step_price(Decimal("100"), Decimal("101"), 1, 0, tick_size=STK)


class TestCalcStepPriceFut:
    def test_es_walker_snaps_to_quarter(self):
        bid, ask = Decimal("4500.00"), Decimal("4500.50")
        # Every step must be on the 0.25 grid
        for step in range(11):
            p = calc_step_price(bid, ask, step, 10, "BUY", tick_size=ES_TICK)
            assert (p / ES_TICK) % 1 == 0, f"off-tick at step {step}: {p}"

    def test_es_buy_terminal_is_ask(self):
        bid, ask = Decimal("4500.00"), Decimal("4500.50")
        assert calc_step_price(bid, ask, 10, 10, "BUY", tick_size=ES_TICK) == ask


class TestCalcProfitTakerPrice:
    def test_basic_long_profit_taker_stk(self):
        # avg_fill=100, qty=10, profit=50, mult=1 → profit/qty=5 → 105
        assert calc_profit_taker_price(
            Decimal("100.00"), Decimal("10"), Decimal("50"), tick_size=STK
        ) == Decimal("105.00")

    def test_fractional_per_share_profit(self):
        # avg_fill=412.33, qty=100, profit=500 → 412.33 + 5 = 417.33
        assert calc_profit_taker_price(
            Decimal("412.33"), Decimal("100"), Decimal("500"), tick_size=STK
        ) == Decimal("417.33")

    def test_es_multiplier_aware(self):
        # ES: mult=50. Entry 4500, 1 contract, $500 profit target.
        # Needs +$10 price move (since 10 * 50 = 500).
        assert calc_profit_taker_price(
            Decimal("4500.00"), Decimal("1"), Decimal("500"),
            tick_size=ES_TICK, multiplier=ES_MULT,
        ) == Decimal("4510.00")

    def test_es_snaps_to_tick(self):
        # $100 profit on 1 ES @ 4500 → +$2 move → 4502.00 (on tick)
        # $105 profit on 1 ES @ 4500 → +$2.10 → 4502.10 → snap → 4502.00
        result = calc_profit_taker_price(
            Decimal("4500.00"), Decimal("1"), Decimal("105"),
            tick_size=ES_TICK, multiplier=ES_MULT,
        )
        assert (result / ES_TICK) % 1 == 0

    def test_zero_qty_raises(self):
        with pytest.raises(ValueError, match="qty_filled"):
            calc_profit_taker_price(Decimal("100"), Decimal("0"), Decimal("500"), tick_size=STK)

    def test_zero_multiplier_raises(self):
        with pytest.raises(ValueError, match="multiplier"):
            calc_profit_taker_price(
                Decimal("100"), Decimal("1"), Decimal("500"),
                tick_size=STK, multiplier=Decimal("0"),
            )


class TestCalcProfitTakerPriceShort:
    def test_short_profit_taker_lower_than_entry(self):
        assert calc_profit_taker_price_short(
            Decimal("100.00"), Decimal("10"), Decimal("50"), tick_size=STK
        ) == Decimal("95.00")

    def test_es_short_multiplier_aware(self):
        assert calc_profit_taker_price_short(
            Decimal("4500.00"), Decimal("1"), Decimal("500"),
            tick_size=ES_TICK, multiplier=ES_MULT,
        ) == Decimal("4490.00")

    def test_zero_qty_raises(self):
        with pytest.raises(ValueError, match="qty_filled"):
            calc_profit_taker_price_short(Decimal("100"), Decimal("0"), Decimal("50"), tick_size=STK)


class TestCalcSharesFromDollars:
    def test_basic_conversion(self):
        assert calc_shares_from_dollars(Decimal("1000"), Decimal("100"), max_shares=100) == Decimal("10")

    def test_floor_division(self):
        assert calc_shares_from_dollars(Decimal("1001"), Decimal("100"), max_shares=100) == Decimal("10")

    def test_capped_at_max_shares(self):
        assert calc_shares_from_dollars(Decimal("10000"), Decimal("10"), max_shares=10) == Decimal("10")

    def test_zero_price_raises(self):
        with pytest.raises(ValueError, match="price must be positive"):
            calc_shares_from_dollars(Decimal("1000"), Decimal("0"), max_shares=10)

    def test_negative_price_raises(self):
        with pytest.raises(ValueError, match="price must be positive"):
            calc_shares_from_dollars(Decimal("1000"), Decimal("-1"), max_shares=10)

    def test_very_small_amount(self):
        assert calc_shares_from_dollars(Decimal("1"), Decimal("100"), max_shares=10) == Decimal("0")


class TestNotionalValue:
    def test_stk_multiplier_one(self):
        assert notional_value(Decimal("10"), Decimal("100")) == Decimal("1000")

    def test_stk_explicit_multiplier_one(self):
        assert notional_value(Decimal("10"), Decimal("100"), Decimal("1")) == Decimal("1000")

    def test_es_multiplier(self):
        # 2 ES contracts @ 4500 × 50 = 450,000 notional
        assert notional_value(Decimal("2"), Decimal("4500"), ES_MULT) == Decimal("450000")
