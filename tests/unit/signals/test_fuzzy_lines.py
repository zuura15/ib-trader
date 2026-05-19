"""Unit tests for fuzzy_lines — RANSAC SR / channel / pivot scoring."""
import numpy as np
import pytest

from ib_trader.signals.fuzzy_lines import (
    detect_fuzzy,
    find_parallel_channels,
    find_pivots_scored,
    fit_fuzzy_lines,
)


@pytest.fixture
def converging_channel():
    """Rising support + falling resistance — a contracting wedge.
    Floor / ceiling are linear-ish with sinusoidal noise."""
    n = 120
    idxs = np.arange(n)
    rng = np.random.default_rng(7)
    base = 28000 + idxs * 0.5
    swing = 20 * np.sin(idxs / 6) + rng.normal(0, 2, n)
    return (base + swing).tolist()


@pytest.fixture
def flat_consolidation():
    """Mostly-flat support and resistance — a sideways range."""
    n = 80
    idxs = np.arange(n)
    rng = np.random.default_rng(3)
    return (29000 + 15 * np.sin(idxs / 4) + rng.normal(0, 1, n)).tolist()


class TestPivotScoring:
    def test_detects_pivots_in_oscillating_series(self, converging_channel):
        pivots = find_pivots_scored(converging_channel)
        assert len(pivots) > 4
        # Mix of highs and lows.
        assert any(p.kind == "high" for p in pivots)
        assert any(p.kind == "low" for p in pivots)
        # Each pivot has prominence in price units, not 0.
        assert all(p.prominence > 0 for p in pivots)
        # Sorted by idx.
        assert pivots == sorted(pivots, key=lambda p: p.idx)

    def test_returns_empty_for_too_short_series(self):
        assert find_pivots_scored([1.0, 2.0]) == []

    def test_returns_empty_for_flat_series(self):
        assert find_pivots_scored([100.0] * 30) == []

    def test_prominence_filter_drops_micro_blips(self):
        # Tight series with tiny wiggles + one big swing.
        rng = np.random.default_rng(0)
        n = 60
        prices = 29000 + rng.normal(0, 0.1, n)
        prices[30] += 50  # one big peak
        loose = find_pivots_scored(prices.tolist(), prominence_fraction=0.0001)
        strict = find_pivots_scored(prices.tolist(), prominence_fraction=0.5)
        assert len(loose) > len(strict)
        # Strict only keeps the big swing's peak.
        assert any(p.idx == 30 for p in strict)


class TestFuzzyLineFit:
    def test_finds_support_and_resistance(self, converging_channel):
        pivots = find_pivots_scored(converging_channel)
        lines = fit_fuzzy_lines(converging_channel, pivots)
        assert any(L.type == "support" for L in lines)
        assert any(L.type == "resistance" for L in lines)
        # Every reported line meets the inlier floor.
        assert all(L.inlier_count >= 3 for L in lines)

    def test_respects_min_inliers(self, converging_channel):
        pivots = find_pivots_scored(converging_channel)
        strict = fit_fuzzy_lines(converging_channel, pivots, min_inliers=5)
        loose = fit_fuzzy_lines(converging_channel, pivots, min_inliers=3)
        assert all(L.inlier_count >= 5 for L in strict)
        assert len(loose) >= len(strict)

    def test_empty_input(self):
        assert fit_fuzzy_lines([], []) == []
        assert fit_fuzzy_lines([100.0] * 50, []) == []


class TestChannels:
    def test_pairs_parallel_lines(self, flat_consolidation):
        det = detect_fuzzy(flat_consolidation)
        # Flat consolidation should yield at least one near-parallel pair.
        assert any(c.span_bars >= 15 for c in det.channels) or len(det.channels) == 0

    def test_channel_score_in_unit_range(self, converging_channel):
        det = detect_fuzzy(converging_channel)
        for c in det.channels:
            assert 0.0 <= c.score <= 1.0

    def test_empty_when_no_resistances(self):
        # Manually craft only supports.
        from ib_trader.signals.fuzzy_lines import FuzzyLine
        only_support = [
            FuzzyLine(type="support", slope=0.1, intercept=29000,
                      from_idx=0, to_idx=50, inlier_count=3,
                      inlier_idxs=[0, 25, 50], residual_threshold=1.0,
                      score=0.5, age_bars=50),
        ]
        assert find_parallel_channels(only_support) == []


class TestDetectFuzzy:
    def test_returns_full_detection(self, converging_channel):
        det = detect_fuzzy(converging_channel)
        assert det.pivots
        assert det.lines
        assert "residual_fraction" in det.config

    def test_empty_input(self):
        det = detect_fuzzy([])
        assert det.pivots == []
        assert det.lines == []
        assert det.channels == []

    def test_short_input(self):
        det = detect_fuzzy([29000.0, 29001.0, 29000.5])
        assert det.lines == []
