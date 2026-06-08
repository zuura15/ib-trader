"""Unit tests for serial number assignment logic."""
import pytest
from datetime import datetime, timezone

from ib_trader.data.models import TradeGroup, TradeStatus
from ib_trader.data.repository import MAX_SERIAL_NUMBER, TradeRepository


def _now():
    return datetime.now(timezone.utc)


class TestSerialNumberAssignment:
    def test_first_serial_is_0(self, session_factory):
        repo = TradeRepository(session_factory)
        assert repo.next_serial_number() == 0

    def test_second_serial_is_1(self, session_factory):
        repo = TradeRepository(session_factory)
        repo.create(TradeGroup(
            serial_number=0, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        assert repo.next_serial_number() == 1

    def test_gaps_are_filled(self, session_factory):
        repo = TradeRepository(session_factory)
        for s in [0, 1, 2, 4, 5]:  # Gap at 3
            repo.create(TradeGroup(
                serial_number=s, symbol="MSFT", direction="LONG",
                status=TradeStatus.OPEN, opened_at=_now(),
            ))
        assert repo.next_serial_number() == 3

    def test_lowest_available_reused(self, session_factory):
        repo = TradeRepository(session_factory)
        for s in [1, 2, 3]:
            repo.create(TradeGroup(
                serial_number=s, symbol="MSFT", direction="LONG",
                status=TradeStatus.OPEN, opened_at=_now(),
            ))
        # 0 is available (lowest)
        assert repo.next_serial_number() == 0

    def test_max_minus_one_is_a_valid_serial(self, session_factory, monkeypatch):
        """The exclusive upper bound means ``MAX_SERIAL_NUMBER - 1`` is
        the highest legal serial. Shrinks the cap to 10 via monkeypatch
        so the test does 9 inserts instead of 99,999."""
        monkeypatch.setattr("ib_trader.data.repository.MAX_SERIAL_NUMBER", 10)
        repo = TradeRepository(session_factory)
        for s in range(9):
            repo.create(TradeGroup(
                serial_number=s, symbol="MSFT", direction="LONG",
                status=TradeStatus.OPEN, opened_at=_now(),
            ))
        assert repo.next_serial_number() == 9

    def test_all_serials_used_raises(self, session_factory, monkeypatch):
        """Same shrink-to-10 trick — verifies the exhaustion error fires
        without inserting 100,000 rows."""
        monkeypatch.setattr("ib_trader.data.repository.MAX_SERIAL_NUMBER", 10)
        repo = TradeRepository(session_factory)
        for s in range(10):
            repo.create(TradeGroup(
                serial_number=s, symbol="MSFT", direction="LONG",
                status=TradeStatus.OPEN, opened_at=_now(),
            ))
        with pytest.raises(RuntimeError, match="serial numbers"):
            repo.next_serial_number()

    def test_default_cap_is_100k(self):
        """Pin the cap so a future accidental change is visible. The
        operator hit the old 1000 cap mid-trade on 2026-06-07; reverting
        to a small cap would re-introduce that incident."""
        assert MAX_SERIAL_NUMBER == 100_000
