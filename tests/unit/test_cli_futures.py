"""Parser coverage for futures shorthand + explicit flags (Epic 1 D2)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from ib_trader.repl.commands import (
    BuyCommand,
    SellCommand,
    parse_command,
)


class TestShorthand:
    def test_buy_es_z26_routes_to_fut(self):
        cmd = parse_command("buy ES Z26 2 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "FUT"
        assert cmd.expiry == "202612"
        assert cmd.symbol == "ES"
        assert cmd.qty == Decimal("2")
        assert cmd.exchange == "CME"

    def test_sell_mes_h27(self):
        cmd = parse_command("sell MES H27 1 market")
        assert isinstance(cmd, SellCommand)
        assert cmd.security_type == "FUT"
        assert cmd.expiry == "202703"
        assert cmd.symbol == "MES"

    def test_stock_unchanged(self):
        cmd = parse_command("buy META 10 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "STK"
        assert cmd.expiry is None
        assert cmd.symbol == "META"

    def test_non_futures_root_with_month_like_token_not_routed(self):
        # AAPL Z26 isn't valid — AAPL isn't in the futures-roots allowlist,
        # so the parser treats Z26 as the QTY and emits the usual error.
        cmd = parse_command("buy AAPL Z26 mid")
        assert cmd is None  # parse error (Z26 isn't a valid qty)


class TestExplicitFlags:
    def test_explicit_sec_type_beats_shorthand_absence(self):
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
        cmd = parse_command(f"buy ES {code} 1 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "FUT"

    def test_case_insensitive_month_code(self):
        cmd = parse_command("buy ES z26 1 mid")
        assert isinstance(cmd, BuyCommand)
        assert cmd.security_type == "FUT"
        assert cmd.expiry == "202612"
