"""Tests for orderRef encoding and decoding."""
from unittest.mock import patch

import pytest

from ib_trader.engine.order_ref import encode, decode, OrderRefInfo


# Stable hostname for assertions — patched into environment.hostname so
# tests don't depend on whichever box runs them.
_TEST_HOST = "test-host"


@pytest.fixture(autouse=True)
def _stable_hostname():
    """Patch the hostname source so encoded refs are deterministic."""
    with patch("ib_trader.engine.order_ref.environment.hostname",
               return_value=_TEST_HOST):
        yield


class TestEncode:
    """Tests for orderRef encoding."""

    def test_basic_encode(self):
        ref = encode("saw-rsi", "QQQ", "B", 42)
        assert ref == f"IBT:{_TEST_HOST}:saw-rsi:QQQ:B:42"

    def test_manual_order(self):
        ref = encode("manual", "AAPL", "S", 0)
        assert ref == f"IBT:{_TEST_HOST}:manual:AAPL:S:0"

    def test_sell_side(self):
        ref = encode("ctr-qqq", "META", "S", 999)
        assert ref == f"IBT:{_TEST_HOST}:ctr-qqq:META:S:999"

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
        """Worst case: 20-char bot_ref + 20-char symbol + host = ~75 chars."""
        ref = encode("a" * 20, "B" * 20, "B", 999)
        assert len(ref) <= 128

    def test_rejects_overlength(self):
        """orderRef must not exceed 128 chars."""
        with pytest.raises(ValueError, match="exceeds 128"):
            encode("a" * 60, "B" * 60, "B", 999)


class TestDecode:
    """Tests for orderRef decoding."""

    def test_basic_decode(self):
        info = decode("IBT:local-prod:saw-rsi:QQQ:B:42")
        assert info == OrderRefInfo(
            host="local-prod", bot_ref="saw-rsi", symbol="QQQ", side="B", serial=42,
        )

    def test_round_trip(self):
        ref = encode("ctr-qqq", "META", "S", 7)
        info = decode(ref)
        assert info.host == _TEST_HOST
        assert info.bot_ref == "ctr-qqq"
        assert info.symbol == "META"
        assert info.side == "S"
        assert info.serial == 7

    def test_manual_order_detection(self):
        ref = encode("manual", "AAPL", "B", 0)
        info = decode(ref)
        assert info.is_manual is True

    def test_bot_order_not_manual(self):
        info = decode("IBT:local-prod:saw-rsi:QQQ:B:42")
        assert info.is_manual is False

    def test_returns_none_for_empty(self):
        assert decode("") is None
        assert decode(None) is None

    def test_returns_none_for_non_ibt(self):
        assert decode("SOME:other:ref") is None

    def test_returns_none_for_legacy_5_part_format(self):
        """Old format without host segment is treated as foreign so we
        don't accidentally claim ownership of pre-migration tags."""
        assert decode("IBT:saw-rsi:QQQ:B:42") is None

    def test_returns_none_for_wrong_part_count(self):
        assert decode("IBT:host:saw-rsi:QQQ") is None
        assert decode("IBT:host:saw-rsi:QQQ:B:42:extra") is None

    def test_returns_none_for_invalid_side(self):
        assert decode("IBT:host:saw-rsi:QQQ:X:42") is None

    def test_returns_none_for_non_integer_serial(self):
        assert decode("IBT:host:saw-rsi:QQQ:B:abc") is None

    def test_serial_zero(self):
        info = decode("IBT:host:saw-rsi:QQQ:B:0")
        assert info.serial == 0

    def test_large_serial(self):
        info = decode("IBT:host:saw-rsi:QQQ:B:999")
        assert info.serial == 999


class TestIsLocal:
    """is_local property — used by reconciler to filter foreign orders."""

    def test_local_when_host_matches(self):
        info = decode(f"IBT:{_TEST_HOST}:saw-rsi:QQQ:B:42")
        assert info.is_local is True

    def test_foreign_when_host_differs(self):
        info = decode("IBT:other-box:saw-rsi:QQQ:B:42")
        assert info.is_local is False
