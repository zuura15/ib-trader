"""Unit tests for ib_trader/utils/symbol.py."""
from __future__ import annotations

from datetime import date

import pytest

from ib_trader.utils import symbol


ALL_MONTH_CODES = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]


class TestMonthCodeTable:
    def test_every_month_has_a_code(self) -> None:
        for m in range(1, 13):
            code = symbol.month_to_code(m)
            assert symbol.code_to_month(code) == m

    def test_every_code_round_trips(self) -> None:
        for code in ALL_MONTH_CODES:
            m = symbol.code_to_month(code)
            assert symbol.month_to_code(m) == code

    @pytest.mark.parametrize("bad", [0, 13, -1])
    def test_rejects_out_of_range_month(self, bad: int) -> None:
        with pytest.raises(ValueError, match="out of range"):
            symbol.month_to_code(bad)

    def test_rejects_unknown_letter(self) -> None:
        with pytest.raises(ValueError, match="unknown futures month code"):
            symbol.code_to_month("A")


class TestParseMonthCode:
    def test_two_digit_year(self) -> None:
        assert symbol.parse_month_code("Z26") == (12, 26)
        assert symbol.parse_month_code("H27") == (3, 27)

    def test_accepts_lowercase(self) -> None:
        assert symbol.parse_month_code("z26") == (12, 26)

    def test_one_digit_year_widens_to_current_decade(self) -> None:
        current_yy = date.today().year % 100
        decade_start = current_yy - (current_yy % 10)
        # the digit matching the current year's units should always resolve
        # to the current year (never the prior decade)
        digit = current_yy % 10
        _month, yy = symbol.parse_month_code(f"Z{digit}")
        assert yy >= current_yy - 1
        assert yy < decade_start + 10 + 10

    @pytest.mark.parametrize("bad", ["", "A26", "Z", "Z266", "ZAB", "Z-1"])
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError):
            symbol.parse_month_code(bad)


class TestExpiryToMonthYear:
    def test_yyyymm(self) -> None:
        assert symbol.expiry_to_month_year("202612") == (12, 26)

    def test_yyyymmdd(self) -> None:
        assert symbol.expiry_to_month_year("20261218") == (12, 26)

    @pytest.mark.parametrize("bad", ["2026", "20261", "2026121", "abcdef", "202613"])
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError):
            symbol.expiry_to_month_year(bad)


class TestFormatDisplaySymbol:
    def test_stock_passes_through(self) -> None:
        assert symbol.format_display_symbol("META", "STK", None) == "META"
        assert symbol.format_display_symbol("META", "stk", "ignored") == "META"

    def test_future_uses_two_digit_year(self) -> None:
        assert symbol.format_display_symbol("ES", "FUT", "202612") == "ES Z26"
        assert symbol.format_display_symbol("MES", "FUT", "20270319") == "MES H27"

    def test_future_requires_expiry(self) -> None:
        with pytest.raises(ValueError, match="requires an expiry"):
            symbol.format_display_symbol("ES", "FUT", None)


class TestFormatIbPasteSymbol:
    def test_stock_passes_through(self) -> None:
        assert symbol.format_ib_paste_symbol("META", "STK", None) == "META"

    def test_future_uses_single_digit_year(self) -> None:
        assert symbol.format_ib_paste_symbol("ES", "FUT", "202612") == "ESZ6"
        assert symbol.format_ib_paste_symbol("MES", "FUT", "20270319") == "MESH7"

    def test_future_requires_expiry(self) -> None:
        with pytest.raises(ValueError):
            symbol.format_ib_paste_symbol("ES", "FUT", None)
