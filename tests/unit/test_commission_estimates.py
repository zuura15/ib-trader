"""Unit tests for ``ib_trader.data.commission_estimates``."""
from __future__ import annotations

from decimal import Decimal

import pytest

from ib_trader.data.commission_estimates import (
    effective_commission,
    estimate_one_side,
    estimate_round_trip,
    root_of,
)


class TestRootExtraction:
    @pytest.mark.parametrize("sym,root", [
        ("GCQ6", "GC"),
        ("MGCQ6", "MGC"),
        ("ESM6", "ES"),
        ("MESM6", "MES"),
        ("NQM6", "NQ"),
        ("MNQM6", "MNQ"),
        ("CLN6", "CL"),
        ("MCLM6", "MCL"),
        # 2-digit year
        ("GCM26", "GC"),
        ("MNQU26", "MNQ"),
        # Lowercase / whitespace tolerated
        ("  gcq6 ", "GC"),
        # Non-futures: returned as-is
        ("AAPL", "AAPL"),
        ("SPY", "SPY"),
    ])
    def test_root_extraction(self, sym: str, root: str) -> None:
        assert root_of(sym) == root


class TestOneSideEstimates:
    def test_full_es_per_side_at_qty_1(self) -> None:
        # ES per-side rate is $2.05 × 1 contract
        assert estimate_one_side("ESM6", 1) == Decimal("2.05")

    def test_full_nq_per_side_at_qty_3(self) -> None:
        assert estimate_one_side("NQM6", 3) == Decimal("6.15")

    def test_full_gc_per_side_at_qty_1(self) -> None:
        assert estimate_one_side("GCQ6", 1) == Decimal("2.21")

    def test_micro_mes_per_side(self) -> None:
        assert estimate_one_side("MESM6", 1) == Decimal("0.47")

    def test_micro_mgc_per_side(self) -> None:
        assert estimate_one_side("MGCQ6", 1) == Decimal("0.50")

    def test_unknown_symbol_returns_zero(self) -> None:
        # STK / unknown root: zero (no estimate available)
        assert estimate_one_side("AAPL", 1) == Decimal("0")

    def test_qty_decimal_input(self) -> None:
        assert estimate_one_side("ESM6", Decimal("2")) == Decimal("4.10")

    def test_qty_string_input(self) -> None:
        assert estimate_one_side("ESM6", "2") == Decimal("4.10")


class TestRoundTripEstimates:
    def test_full_nq_round_trip_qty_1(self) -> None:
        # NQ round-trip = 2 × $2.05 = $4.10
        assert estimate_round_trip("NQM6", 1) == Decimal("4.10")

    def test_full_gc_round_trip_qty_1(self) -> None:
        # GC round-trip = 2 × $2.21 = $4.42
        assert estimate_round_trip("GCQ6", 1) == Decimal("4.42")

    def test_full_es_round_trip_qty_5(self) -> None:
        assert estimate_round_trip("ESM6", 5) == Decimal("20.50")


class TestEffectiveCommission:
    def test_uses_reported_when_nonzero(self) -> None:
        # Reported takes precedence over estimate
        assert effective_commission(Decimal("3.50"), "ESM6", 1) == Decimal("3.50")

    def test_falls_back_to_estimate_when_zero(self) -> None:
        assert effective_commission(Decimal("0"), "ESM6", 1) == Decimal("4.10")

    def test_falls_back_to_estimate_when_none(self) -> None:
        assert effective_commission(None, "GCQ6", 1) == Decimal("4.42")

    def test_round_trip_false_uses_one_side(self) -> None:
        assert effective_commission(
            None, "ESM6", 1, round_trip=False,
        ) == Decimal("2.05")

    def test_reported_takes_precedence_even_for_unknown_symbol(self) -> None:
        assert effective_commission(Decimal("1.00"), "AAPL", 100) == Decimal("1.00")

    def test_unknown_symbol_with_zero_returns_zero(self) -> None:
        # No estimate available → 0 (caller decides next step)
        assert effective_commission(None, "AAPL", 100) == Decimal("0")
        assert effective_commission(Decimal("0"), "AAPL", 100) == Decimal("0")
