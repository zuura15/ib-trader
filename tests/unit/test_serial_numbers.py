"""Unit tests for serial number assignment logic."""
import pytest
from datetime import datetime, timezone

from ib_trader.data.models import TradeGroup, TradeStatus
from ib_trader.data.repository import TradeRepository


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

    def test_serial_999_boundary(self, session_factory):
        """Verify that 999 is a valid serial number."""
        repo = TradeRepository(session_factory)
        for s in range(999):
            repo.create(TradeGroup(
                serial_number=s, symbol="MSFT", direction="LONG",
                status=TradeStatus.OPEN, opened_at=_now(),
            ))
        # Only 999 remains
        assert repo.next_serial_number() == 999

    def test_all_serials_used_raises(self, session_factory):
        repo = TradeRepository(session_factory)
        for s in range(1000):
            repo.create(TradeGroup(
                serial_number=s, symbol="MSFT", direction="LONG",
                status=TradeStatus.OPEN, opened_at=_now(),
            ))
        with pytest.raises(RuntimeError, match="serial numbers"):
            repo.next_serial_number()
