"""Tests for orderRef encoding and decoding."""
import pytest

from ib_trader.engine.order_ref import encode, decode, OrderRefInfo


class TestEncode:
    """Tests for orderRef encoding."""

    def test_basic_encode(self):
        ref = encode("saw-rsi", "QQQ", "B", 42)
        assert ref == "IBT:saw-rsi:QQQ:B:42"

    def test_manual_order(self):
        ref = encode("manual", "AAPL", "S", 0)
        assert ref == "IBT:manual:AAPL:S:0"

    def test_sell_side(self):
        ref = encode("ctr-qqq", "META", "S", 999)
        assert ref == "IBT:ctr-qqq:META:S:999"

    def test_rejects_invalid_side(self):
        with pytest.raises(ValueError, match="side must be"):
            encode("saw-rsi", "QQQ", "BUY", 1)

    def test_rejects_separator_in_bot_ref(self):
        with pytest.raises(ValueError, match="bot_ref must not contain"):
            encode("saw:rsi", "QQQ", "B", 1)

    def test_rejects_separator_in_symbol(self):
        with pytest.raises(ValueError, match="symbol must not contain"):
            encode("saw-rsi", "QQQ:X", "B", 1)

    def test_max_length_within_limit(self):
        """Worst case: 20-char bot_ref + 20-char symbol = 51 chars total."""
        ref = encode("a" * 20, "B" * 20, "B", 999)
        assert len(ref) <= 128

    def test_rejects_overlength(self):
        """orderRef must not exceed 128 chars."""
        with pytest.raises(ValueError, match="exceeds 128"):
            encode("a" * 60, "B" * 60, "B", 999)


class TestDecode:
    """Tests for orderRef decoding."""

    def test_basic_decode(self):
        info = decode("IBT:saw-rsi:QQQ:B:42")
        assert info == OrderRefInfo(bot_ref="saw-rsi", symbol="QQQ", side="B", serial=42)

    def test_round_trip(self):
        ref = encode("ctr-qqq", "META", "S", 7)
        info = decode(ref)
        assert info.bot_ref == "ctr-qqq"
        assert info.symbol == "META"
        assert info.side == "S"
        assert info.serial == 7

    def test_manual_order_detection(self):
        ref = encode("manual", "AAPL", "B", 0)
        info = decode(ref)
        assert info.is_manual is True

    def test_bot_order_not_manual(self):
        info = decode("IBT:saw-rsi:QQQ:B:42")
        assert info.is_manual is False

    def test_returns_none_for_empty(self):
        assert decode("") is None
        assert decode(None) is None

    def test_returns_none_for_non_ibt(self):
        assert decode("SOME:other:ref") is None

    def test_returns_none_for_wrong_part_count(self):
        assert decode("IBT:saw-rsi:QQQ") is None
        assert decode("IBT:saw-rsi:QQQ:B:42:extra") is None

    def test_returns_none_for_invalid_side(self):
        assert decode("IBT:saw-rsi:QQQ:X:42") is None

    def test_returns_none_for_non_integer_serial(self):
        assert decode("IBT:saw-rsi:QQQ:B:abc") is None

    def test_serial_zero(self):
        info = decode("IBT:saw-rsi:QQQ:B:0")
        assert info.serial == 0

    def test_large_serial(self):
        info = decode("IBT:saw-rsi:QQQ:B:999")
        assert info.serial == 999
