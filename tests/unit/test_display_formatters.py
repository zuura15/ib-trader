"""Tests for the console display formatters in engine/order.py.

The bug (2026-06-01): close-fill output read ``$4513.933333333333333333333333``
and ``+$35.0900000000000000000000100`` — raw ``str(Decimal)`` output from
weighted-average division and money arithmetic, no thousand separators
on 4-5 digit futures notionals.

These tests pin: 2-dp money, 4-dp price (trailing zeros trimmed),
thousand separators, signed/zero/negative handling, and that
``_fmt_qty`` keeps the existing ``3.0 → 3`` integer strip.
"""
from __future__ import annotations

from decimal import Decimal

from ib_trader.engine.order import _fmt_money, _fmt_price, _fmt_qty


class TestFmtMoney:
    def test_basic_two_decimals(self):
        assert _fmt_money(Decimal("42")) == "42.00"
        assert _fmt_money(Decimal("42.5")) == "42.50"
        assert _fmt_money(Decimal("42.555")) == "42.56"  # half-up rounding

    def test_pnl_arithmetic_artifacts_collapsed(self):
        # This is the user's exact reported P&L string.
        assert _fmt_money(Decimal("35.0900000000000000000000100")) == "35.09"

    def test_thousand_separator(self):
        assert _fmt_money(Decimal("1234.56")) == "1,234.56"
        assert _fmt_money(Decimal("1234567.89")) == "1,234,567.89"

    def test_zero(self):
        assert _fmt_money(Decimal("0")) == "0.00"

    def test_negative_keeps_sign(self):
        assert _fmt_money(Decimal("-42.5")) == "-42.50"
        assert _fmt_money(Decimal("-1234.56")) == "-1,234.56"

    def test_custom_places(self):
        assert _fmt_money(Decimal("1.23456"), places=4) == "1.2346"

    def test_garbage_input_returns_str(self):
        assert _fmt_money("not-a-number") == "not-a-number"


class TestFmtPrice:
    def test_avg_price_division_artifact_collapsed(self):
        # This is the user's exact reported avg_price string from a
        # 3-fill weighted-average division.
        assert _fmt_price(Decimal("4513.933333333333333333333333")) == "4,513.9333"

    def test_trailing_zeros_stripped(self):
        # 4514.10 → "4,514.1" (don't pad with cosmetic zeros on price).
        assert _fmt_price(Decimal("4514.10")) == "4,514.1"
        assert _fmt_price(Decimal("4514.00")) == "4,514"

    def test_subcent_tick_preserved(self):
        # ES futures: $0.25 tick → 4514.25 must keep its .25.
        assert _fmt_price(Decimal("4514.25")) == "4,514.25"

    def test_subbasis_tick_preserved(self):
        # MGCQ6 tick = $0.10 → 4514.1 round-trips, no extra zeros.
        assert _fmt_price(Decimal("4514.1")) == "4,514.1"

    def test_four_decimal_precision(self):
        # FX-ish ticks like 1.23456 round to 4 places.
        assert _fmt_price(Decimal("1.23456")) == "1.2346"

    def test_zero(self):
        # All-zero collapses to "0" after the trailing-zero strip.
        assert _fmt_price(Decimal("0")) == "0"

    def test_negative(self):
        assert _fmt_price(Decimal("-1234.5")) == "-1,234.5"


class TestFmtQty:
    def test_integer_strips_trailing_zero(self):
        # Existing contract (pinned here so the formatter consolidation
        # doesn't accidentally regress it).
        assert _fmt_qty(Decimal("3.0")) == "3"
        assert _fmt_qty(Decimal("621.0")) == "621"

    def test_fractional_kept(self):
        assert _fmt_qty(Decimal("3.5")) == "3.5"
        assert _fmt_qty(Decimal("0.25")) == "0.25"

    def test_garbage_returns_str(self):
        class _NotADecimal:
            def __str__(self):
                return "<garbage>"
        assert _fmt_qty(_NotADecimal()) == "<garbage>"
