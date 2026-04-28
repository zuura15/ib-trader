"""Unit tests for ib_trader/engine/ticks.py."""
from __future__ import annotations

from decimal import Decimal

import pytest

from ib_trader.engine.ticks import is_on_tick, snap_for_stk, snap_to_tick


class TestSnapToTickNearest:
    def test_stk_rounds_up_half(self) -> None:
        assert snap_to_tick(Decimal("10.125"), Decimal("0.01"), "nearest") == Decimal("10.13")

    def test_stk_rounds_down(self) -> None:
        assert snap_to_tick(Decimal("10.124"), Decimal("0.01"), "nearest") == Decimal("10.12")

    def test_es_quarter_tick_nearest(self) -> None:
        assert snap_to_tick(Decimal("4500.10"), Decimal("0.25"), "nearest") == Decimal("4500.00")
        assert snap_to_tick(Decimal("4500.13"), Decimal("0.25"), "nearest") == Decimal("4500.25")
        assert snap_to_tick(Decimal("4500.125"), Decimal("0.25"), "nearest") == Decimal("4500.25")

    def test_fx_tick(self) -> None:
        assert snap_to_tick(Decimal("1.23455"), Decimal("0.0001"), "nearest") == Decimal("1.2346")


class TestSnapToTickDirections:
    def test_up_floors_to_next_tick(self) -> None:
        assert snap_to_tick(Decimal("4500.01"), Decimal("0.25"), "up") == Decimal("4500.25")

    def test_down_floors_to_prior_tick(self) -> None:
        assert snap_to_tick(Decimal("4500.24"), Decimal("0.25"), "down") == Decimal("4500.00")

    def test_on_tick_value_unchanged(self) -> None:
        for d in ("nearest", "up", "down"):
            assert snap_to_tick(Decimal("4500.25"), Decimal("0.25"), d) == Decimal("4500.25")  # type: ignore[arg-type]


class TestSnapRejections:
    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-0.01")])
    def test_rejects_non_positive_tick(self, bad: Decimal) -> None:
        with pytest.raises(ValueError, match="tick_size must be positive"):
            snap_to_tick(Decimal("1"), bad, "nearest")

    def test_rejects_unknown_direction(self) -> None:
        with pytest.raises(ValueError, match="unknown direction"):
            snap_to_tick(Decimal("1"), Decimal("0.01"), "sideways")  # type: ignore[arg-type]


class TestIsOnTick:
    def test_on_tick(self) -> None:
        assert is_on_tick(Decimal("4500.25"), Decimal("0.25"))
        assert is_on_tick(Decimal("10.12"), Decimal("0.01"))

    def test_off_tick(self) -> None:
        assert not is_on_tick(Decimal("4500.10"), Decimal("0.25"))
        assert not is_on_tick(Decimal("10.125"), Decimal("0.01"))


class TestSnapForStk:
    def test_stk_wrapper(self) -> None:
        assert snap_for_stk(Decimal("10.125")) == Decimal("10.13")
        assert snap_for_stk(Decimal("10.124")) == Decimal("10.12")
        assert snap_for_stk(Decimal("10.126"), "down") == Decimal("10.12")
