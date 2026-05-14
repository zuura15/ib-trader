"""Unit tests for ``find_wedges`` + ``TrendLine.third_touch_idx`` —
the canonical SR pieces that ship to both the bot and the chart.
"""
from __future__ import annotations

from ib_trader.signals.sr_fan import (
    TrendLine, find_wedges, detect_lines,
    NEAR_TOUCH_TOLERANCE_FRACTION,
)


def _line(
    type_: str, slope: float, intercept: float,
    from_idx: int, anchor_b_idx: int, to_idx: int,
    *, broken: bool = False, touches: int = 3,
) -> TrendLine:
    return TrendLine(
        type=type_, from_idx=from_idx, anchor_b_idx=anchor_b_idx,
        to_idx=to_idx, slope=slope, intercept=intercept,
        touches=touches, break_idx=to_idx if broken else None,
    )


class TestFindWedgesBasic:
    def test_empty_inputs(self):
        assert find_wedges([], [], 100) == []

    def test_only_supports(self):
        s = [_line("support", 0.5, 100, 0, 5, 20)]
        assert find_wedges(s, [], 20) == []

    def test_only_resistances(self):
        r = [_line("resistance", -0.5, 200, 0, 5, 20)]
        assert find_wedges([], r, 20) == []


class TestConvergenceRules:
    def test_diverging_pair_rejected(self):
        # support slope < resistance slope → diverging
        s = [_line("support", -0.5, 100, 0, 5, 20)]
        r = [_line("resistance", 0.5, 200, 0, 5, 20)]
        assert find_wedges(s, r, 20) == []

    def test_parallel_pair_rejected(self):
        s = [_line("support", 0.5, 100, 0, 5, 20)]
        r = [_line("resistance", 0.5, 200, 0, 5, 20)]
        assert find_wedges(s, r, 20) == []

    def test_converging_pair_returns_wedge(self):
        # support 0.5/bar, resistance -0.5/bar, gap closes at idx=100.
        s = [_line("support", 0.5, 100, 0, 5, 20)]
        r = [_line("resistance", -0.5, 200, 0, 5, 20)]
        w = find_wedges(s, r, 20)
        assert len(w) == 1
        # apex_idx = (200 - 100)/(0.5 - (-0.5)) = 100. Bars ahead = 80.
        assert w[0].apex_bars_ahead == 80


class TestApexRangeGate:
    def test_apex_in_past_rejected(self):
        # Lines crossed already at idx=5; last_idx=20.
        s = [_line("support", 1.0, 0, 0, 3, 20)]
        r = [_line("resistance", -1.0, 10, 0, 3, 20)]
        assert find_wedges(s, r, 20) == []

    def test_apex_too_far_rejected(self):
        # apex at idx ~10000; max_apex_bars_ahead=200 default.
        s = [_line("support", 0.001, 100, 0, 5, 20)]
        r = [_line("resistance", -0.001, 110, 0, 5, 20)]
        assert find_wedges(s, r, 20) == []

    def test_apex_within_custom_max(self):
        s = [_line("support", 0.01, 100, 0, 5, 20)]
        r = [_line("resistance", -0.01, 110, 0, 5, 20)]
        # apex_idx = 10/0.02 = 500. Bars ahead = 480. default 200 cap → no.
        assert find_wedges(s, r, 20) == []
        # Raise cap → wedge found.
        w = find_wedges(s, r, 20, max_apex_bars_ahead=600)
        assert len(w) == 1


class TestOverlapGate:
    def test_overlap_below_min_rejected(self):
        # Lines overlap on [10, 12] = 2 bars; default min_overlap=5.
        s = [_line("support", 0.5, 100, 10, 11, 12)]
        r = [_line("resistance", -0.5, 200, 10, 11, 12)]
        assert find_wedges(s, r, 12) == []

    def test_overlap_above_min_accepted(self):
        s = [_line("support", 0.5, 100, 0, 5, 20)]
        r = [_line("resistance", -0.5, 200, 0, 5, 20)]
        # overlap = 20 - 0 = 20 ≥ 5 ✓
        assert len(find_wedges(s, r, 20)) == 1


class TestBrokenLineExclusion:
    def test_broken_support_excluded_by_default(self):
        s = [_line("support", 0.5, 100, 0, 5, 20, broken=True)]
        r = [_line("resistance", -0.5, 200, 0, 5, 20)]
        assert find_wedges(s, r, 20) == []

    def test_broken_resistance_excluded_by_default(self):
        s = [_line("support", 0.5, 100, 0, 5, 20)]
        r = [_line("resistance", -0.5, 200, 0, 5, 20, broken=True)]
        assert find_wedges(s, r, 20) == []

    def test_include_broken_true(self):
        s = [_line("support", 0.5, 100, 0, 5, 20, broken=True)]
        r = [_line("resistance", -0.5, 200, 0, 5, 20)]
        assert len(find_wedges(s, r, 20, include_broken=True)) == 1


class TestCrossedLines:
    def test_r_not_above_s_rejected(self):
        # At overlapStart, resistance line price < support line price.
        # Should reject as a non-wedge shape.
        s = [_line("support", 0.5, 300, 0, 5, 20)]   # at idx 0: price=300
        r = [_line("resistance", -0.5, 200, 0, 5, 20)]  # at idx 0: price=200
        assert find_wedges(s, r, 20) == []


class TestSortOrder:
    def test_nearest_apex_first(self):
        # Two wedges; ensure result is sorted ascending by apex.
        s_near = _line("support", 0.5, 100, 0, 5, 20)
        r_near = _line("resistance", -0.5, 200, 0, 5, 20)  # apex 80 bars
        s_far = _line("support", 0.1, 100, 0, 5, 20)
        r_far = _line("resistance", -0.1, 130, 0, 5, 20)  # apex 130 bars
        w = find_wedges([s_near, s_far], [r_near, r_far], 20)
        # Multiple pair combinations may occur; first must have
        # smallest apex.
        assert len(w) >= 1
        assert w[0].apex_bars_ahead <= w[-1].apex_bars_ahead
        for i in range(len(w) - 1):
            assert w[i].apex_bars_ahead <= w[i + 1].apex_bars_ahead


class TestThirdTouchIdxOnDetectLines:
    def test_third_touch_recorded(self):
        # Construct closes that form a 3-touch support line on a
        # rising trend with strict touches at indices 0, 4, 8.
        # Linear support with slope 0.5/bar, pivots at every 4 bars.
        closes = [
            100.0, 101.0, 102.0, 101.5,           # 0: pivot low
            102.0, 103.0, 104.0, 103.5,           # 4: pivot low
            104.0, 105.0, 106.0, 105.5,           # 8: pivot low (3rd)
            106.0, 107.0, 108.0,                  # past P
        ]
        lines = detect_lines(closes, up_to=len(closes) - 1, type_="support")
        confirmed = [ln for ln in lines if ln.touches >= 3]
        assert confirmed, "expected at least one confirmed 3-touch line"
        # At least one of the confirmed lines must have its 3rd
        # strict-touch idx recorded.
        with_third = [ln for ln in confirmed
                      if ln.third_touch_idx is not None]
        assert with_third, (
            "expected third_touch_idx populated on at least one "
            "confirmed line"
        )
        # The 3rd-touch idx must be at or after P (it's either P
        # itself when P happens to be the 3rd strict pivot found, or
        # a post-P confirmation pivot). Mirrors the TS semantic at
        # ``supportResistance.ts:262``.
        for ln in with_third:
            assert ln.third_touch_idx >= ln.anchor_b_idx

    def test_two_touch_line_has_none_third_touch(self):
        # Just 2 pivots → 2-touch line at most.
        closes = [100.0, 101.0, 100.0, 102.0, 101.0]
        lines = detect_lines(closes, up_to=len(closes) - 1, type_="support")
        for ln in lines:
            if ln.touches < 3:
                assert ln.third_touch_idx is None


class TestNearTouchConstantExposed:
    def test_near_touch_default_is_five_times_strict(self):
        from ib_trader.signals.sr_fan import TOUCH_TOLERANCE_FRACTION
        assert NEAR_TOUCH_TOLERANCE_FRACTION == 5 * TOUCH_TOLERANCE_FRACTION
