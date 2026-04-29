"""Parser coverage for futures shorthand + explicit flags (Epic 1 D2).

Futures use the IB-paste localSymbol form as a single token —
``ESZ26`` / ``MESM6`` / ``GCM26``. The parser detects the pattern but
keeps the symbol intact so it matches the form IB displays everywhere.
The engine resolves the contract via ``Contract(localSymbol=...)``.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from ib_trader.repl.commands import (
    BuyCommand,
    SellCommand,
    parse_command,
)


class TestShorthand:
    def test_buy_esz26_routes_to_fut(self):
        cmd = parse_command("buy ESZ26 2 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "FUT"
        # localSymbol form: engine resolves expiry from IB at qualify time
        assert cmd.expiry is None
        assert cmd.symbol == "ESZ26"
        assert cmd.qty == Decimal("2")

    def test_sell_mesh27(self):
        cmd = parse_command("sell MESH27 1 market")
        assert isinstance(cmd, SellCommand)
        assert cmd.security_type == "FUT"
        assert cmd.expiry is None
        assert cmd.symbol == "MESH27"

    def test_single_digit_year(self):
        # IB-paste short form keeps the year 1-digit when within decade.
        cmd = parse_command("buy ESM6 1 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "FUT"
        assert cmd.symbol == "ESM6"

    def test_stock_unchanged(self):
        cmd = parse_command("buy META 10 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "STK"
        assert cmd.expiry is None
        assert cmd.symbol == "META"

    def test_etf_starting_with_g_not_misclassified(self):
        # GLD starts with 'G' but isn't a known futures root — must
        # stay STK regardless of whatever follows.
        cmd = parse_command("buy GLD 10 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "STK"

    def test_non_futures_root_with_month_like_suffix_not_routed(self):
        # AAPLZ26 doesn't match a known futures root — treated as STK.
        cmd = parse_command("buy AAPLZ26 1 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "STK"


class TestExplicitFlags:
    def test_explicit_sec_type_beats_pattern_detection(self):
        cmd = parse_command("buy ES 1 mid --sec-type FUT --expiry 20261219")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "FUT"
        assert cmd.expiry == "20261219"

    def test_trading_class_flag(self):
        cmd = parse_command("buy ES 1 mid --sec-type FUT --expiry 202612 --trading-class MES")
        assert cmd.trading_class == "MES"

    def test_exchange_flag(self):
        cmd = parse_command("buy ES 1 mid --sec-type FUT --expiry 202612 --exchange NYMEX")
        assert cmd.exchange == "NYMEX"


class TestShorthandEdgeCases:
    @pytest.mark.parametrize("code", ["F26", "G26", "H26", "J26", "K26", "M26", "N26",
                                       "Q26", "U26", "V26", "X26", "Z26"])
    def test_every_month_letter_parses(self, code: str):
        cmd = parse_command(f"buy ES{code} 1 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "FUT"
        assert cmd.symbol == f"ES{code}"

    def test_case_insensitive_localSymbol(self):
        cmd = parse_command("buy esz26 1 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "FUT"
        # Symbol normalized upper-case so the engine sends the form IB expects.
        assert cmd.symbol == "ESZ26"
