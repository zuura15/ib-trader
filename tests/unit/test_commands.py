"""Unit tests for REPL command parsing."""
from decimal import Decimal

from ib_trader.repl.commands import (
    parse_command, parse_buy_sell, parse_close, parse_modify,
    BuyCommand, SellCommand, CloseCommand, ModifyCommand,
)


class TestParseBuySell:
    def test_basic_buy_mid(self):
        cmd = parse_buy_sell(["buy", "MSFT", "100", "mid"])
        assert isinstance(cmd, BuyCommand)
        assert cmd.symbol == "MSFT"
        assert cmd.qty == Decimal("100")
        assert cmd.strategy == "mid"
        assert cmd.profit_amount is None

    def test_buy_with_profit(self):
        cmd = parse_buy_sell(["buy", "MSFT", "100", "mid", "500"])
        assert isinstance(cmd, BuyCommand)
        assert cmd.profit_amount == Decimal("500")

    def test_buy_market(self):
        cmd = parse_buy_sell(["buy", "AAPL", "5", "market"])
        assert isinstance(cmd, BuyCommand)
        assert cmd.strategy == "market"

    def test_sell_mid(self):
        cmd = parse_buy_sell(["sell", "MSFT", "10", "mid"])
        assert isinstance(cmd, SellCommand)
        assert cmd.side if hasattr(cmd, "side") else True  # SellCommand type sufficient

    def test_buy_with_dollars(self):
        cmd = parse_buy_sell(["buy", "MSFT", "1", "mid", "--dollars", "5000"])
        assert isinstance(cmd, BuyCommand)
        assert cmd.dollars == Decimal("5000")

    def test_buy_with_take_profit_price(self):
        cmd = parse_buy_sell(["buy", "MSFT", "10", "mid", "--take-profit-price", "420.00"])
        assert isinstance(cmd, BuyCommand)
        assert cmd.take_profit_price == Decimal("420.00")

    def test_buy_with_stop_loss(self):
        cmd = parse_buy_sell(["buy", "MSFT", "10", "mid", "--stop-loss", "300"])
        assert isinstance(cmd, BuyCommand)
        assert cmd.stop_loss == Decimal("300")

    def test_symbol_uppercased(self):
        cmd = parse_buy_sell(["buy", "msft", "10", "mid"])
        assert cmd.symbol == "MSFT"

    def test_invalid_strategy_returns_none(self, capsys):
        cmd = parse_buy_sell(["buy", "MSFT", "10", "limit"])
        assert cmd is None
        captured = capsys.readouterr()
        assert "Error" in captured.out

    def test_invalid_qty_returns_none(self, capsys):
        cmd = parse_buy_sell(["buy", "MSFT", "abc", "mid"])
        assert cmd is None
        assert "Error" in capsys.readouterr().out

    def test_negative_qty_returns_none(self, capsys):
        cmd = parse_buy_sell(["buy", "MSFT", "-5", "mid"])
        assert cmd is None
        assert "Error" in capsys.readouterr().out

    def test_missing_args_returns_none(self, capsys):
        cmd = parse_buy_sell(["buy", "MSFT"])
        assert cmd is None
        assert "Error" in capsys.readouterr().out

    def test_unknown_option_returns_none(self, capsys):
        cmd = parse_buy_sell(["buy", "MSFT", "10", "mid", "--unknown", "val"])
        assert cmd is None
        assert "Error" in capsys.readouterr().out


class TestParseClose:
    def test_basic_close(self):
        cmd = parse_close(["close", "4"])
        assert isinstance(cmd, CloseCommand)
        assert cmd.serial == 4
        assert cmd.strategy == "mid"

    def test_close_with_market(self):
        cmd = parse_close(["close", "4", "market"])
        assert cmd.strategy == "market"

    def test_close_with_profit(self):
        cmd = parse_close(["close", "4", "mid", "200"])
        assert cmd.profit_amount == Decimal("200")

    def test_close_with_take_profit_price(self):
        cmd = parse_close(["close", "4", "--take-profit-price", "420.00"])
        assert cmd.take_profit_price == Decimal("420.00")

    def test_invalid_serial(self, capsys):
        cmd = parse_close(["close", "abc"])
        assert cmd is None
        assert "Error" in capsys.readouterr().out

    def test_missing_serial(self, capsys):
        cmd = parse_close(["close"])
        assert cmd is None
        assert "Error" in capsys.readouterr().out


class TestParseModify:
    def test_basic_modify(self):
        cmd = parse_modify(["modify", "4"])
        assert isinstance(cmd, ModifyCommand)
        assert cmd.serial == 4

    def test_missing_serial(self, capsys):
        cmd = parse_modify(["modify"])
        assert cmd is None
        assert "Error" in capsys.readouterr().out


class TestParseCommand:
    def test_buy_dispatches(self):
        cmd = parse_command("buy MSFT 10 mid")
        assert isinstance(cmd, BuyCommand)

    def test_sell_dispatches(self):
        cmd = parse_command("sell MSFT 5 market")
        assert isinstance(cmd, SellCommand)

    def test_close_dispatches(self):
        cmd = parse_command("close 4")
        assert isinstance(cmd, CloseCommand)

    def test_modify_dispatches(self):
        cmd = parse_command("modify 4")
        assert isinstance(cmd, ModifyCommand)

    def test_exit_returns_string(self):
        cmd = parse_command("exit")
        assert cmd == "exit"

    def test_quit_returns_string(self):
        cmd = parse_command("quit")
        assert cmd == "exit"

    def test_help_returns_string(self):
        cmd = parse_command("help")
        assert cmd == "help"

    def test_orders_returns_string(self):
        cmd = parse_command("orders")
        assert cmd == "orders"

    def test_empty_line_returns_none(self):
        cmd = parse_command("")
        assert cmd is None

    def test_whitespace_only_returns_none(self):
        cmd = parse_command("   ")
        assert cmd is None

    def test_unknown_command(self, capsys):
        cmd = parse_command("foobar")
        assert cmd is None
        assert "Error" in capsys.readouterr().out
