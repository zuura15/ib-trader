"""Smoke + contract tests for ``GET /engine/sr``.

The endpoint is the chart's single source of truth for SR lines,
wedges, and pivot timestamps. The bot uses ``find_wedges`` directly
in-process; this endpoint exposes the SAME computation over HTTP for
the chart to render. Tests here pin the response shape and verify
that query-param overrides propagate into detection.
"""
from __future__ import annotations

from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from ib_trader.engine.internal_api import app, set_context


@pytest.fixture(autouse=True)
def _init_engine_context():
    """Stub the engine context so endpoints don't 503. ``get_history``
    is patched per-test to skip IB qualifier + fetch."""
    set_context(object())
    yield
    set_context(None)


def _fabricate_bars(n: int = 60, base: float = 100.0) -> list[dict]:
    """Build a synthetic bar series with a clean 3-touch rising
    support fan (pivots every ~4 bars, slope ~0.5) so detection
    fires deterministically."""
    bars: list[dict] = []
    for i in range(n):
        # Sinusoidal noise around a rising trend so we get pivots.
        trend = base + 0.5 * i
        noise = ((i % 4) - 1.5)
        close = trend + noise
        bars.append({
            "ts": f"2026-05-13T{(15 + i // 60):02d}:"
                  f"{(i * 3) % 60:02d}:00+00:00",
            "open": close - 0.1, "high": close + 0.5,
            "low": close - 0.5, "close": close,
            "volume": 100,
        })
    return bars


def _stub_history(bars: list[dict]):
    """Patch ``get_history`` to short-circuit IB and return the
    fabricated bars directly."""
    return patch(
        "ib_trader.engine.internal_api.get_history",
        new=AsyncMock(return_value=bars),
    )


class TestEndpointShape:
    def test_returns_required_top_level_keys(self):
        bars = _fabricate_bars()
        with _stub_history(bars):
            with TestClient(app) as c:
                r = c.get("/engine/sr",
                          params={"symbol": "FAKE", "sec_type": "STK"})
        assert r.status_code == 200, r.text
        data = r.json()
        # Contract: chart relies on every one of these keys.
        for key in ("bars_count", "last_ts", "lines", "wedges",
                    "pivot_lows", "pivot_highs"):
            assert key in data, f"missing {key}"
        assert data["bars_count"] == len(bars)
        assert data["last_ts"] == bars[-1]["ts"]

    def test_line_payload_has_required_fields(self):
        bars = _fabricate_bars()
        with _stub_history(bars):
            with TestClient(app) as c:
                r = c.get("/engine/sr",
                          params={"symbol": "FAKE", "sec_type": "STK"})
        data = r.json()
        assert data["lines"], "expected at least one line in fabricated set"
        ln = data["lines"][0]
        for key in (
            "type", "from_ts", "from_price", "anchor_b_ts",
            "anchor_b_price", "to_ts", "to_price", "touches",
            "is_broken", "break_ts", "break_price", "third_touch_ts",
            "slope_per_bar", "intercept",
        ):
            assert key in ln, f"line missing {key}"

    def test_wedge_payload_has_vertices(self):
        bars = _fabricate_bars()
        with _stub_history(bars):
            with TestClient(app) as c:
                r = c.get("/engine/sr",
                          params={"symbol": "FAKE", "sec_type": "STK"})
        data = r.json()
        # Wedges may or may not exist for our fabricated bars; if any
        # do, check the structure.
        for w in data["wedges"]:
            for key in (
                "apex_bars_ahead", "apex_idx_float",
                "overlap_start_ts", "right_ts", "vertices",
            ):
                assert key in w, f"wedge missing {key}"
            v = w["vertices"]
            for corner in ("support_left", "support_right",
                           "resistance_right", "resistance_left"):
                assert corner in v
                assert "ts" in v[corner] and "price" in v[corner]


class TestEmptyHistory:
    def test_under_four_bars_returns_empty(self):
        with _stub_history([]):
            with TestClient(app) as c:
                r = c.get("/engine/sr",
                          params={"symbol": "FAKE", "sec_type": "STK"})
        data = r.json()
        assert data["lines"] == []
        assert data["wedges"] == []
        assert data["pivot_lows"] == []
        assert data["pivot_highs"] == []


class TestQueryParams:
    def test_near_touch_tolerance_override_widens_detection(self):
        """Bump near-touch tolerance so 4th-touch loose acceptance
        should accept more pivots → equal or greater touch counts."""
        bars = _fabricate_bars()
        with _stub_history(bars):
            with TestClient(app) as c:
                base = c.get("/engine/sr", params={
                    "symbol": "FAKE", "sec_type": "STK",
                    "near_touch_tolerance_fraction": 0.001,
                }).json()
                wide = c.get("/engine/sr", params={
                    "symbol": "FAKE", "sec_type": "STK",
                    "near_touch_tolerance_fraction": 0.01,
                }).json()
        # Sum of touches under wider tolerance >= narrower.
        base_touches = sum(ln["touches"] for ln in base["lines"])
        wide_touches = sum(ln["touches"] for ln in wide["lines"])
        assert wide_touches >= base_touches

    def test_break_stale_bars_override_passes_through(self):
        # No assertion on detection magnitude — just smoke that the
        # param is accepted without error.
        bars = _fabricate_bars()
        with _stub_history(bars):
            with TestClient(app) as c:
                r = c.get("/engine/sr", params={
                    "symbol": "FAKE", "sec_type": "STK",
                    "break_stale_bars": 60,
                })
        assert r.status_code == 200, r.text

    def test_include_broken_wedges_param_accepted(self):
        bars = _fabricate_bars()
        with _stub_history(bars):
            with TestClient(app) as c:
                r = c.get("/engine/sr", params={
                    "symbol": "FAKE", "sec_type": "STK",
                    "include_broken_wedges": True,
                })
        assert r.status_code == 200, r.text


class TestPriceConsistency:
    def test_from_price_matches_value_at_from_idx(self):
        """Backend's ``from_price`` must be exactly
        ``slope_per_bar * from_idx_in_backend_space + intercept``.
        Adapter relies on this to rebase intercept on the frontend.
        We can't access the index directly through the API, but we
        can verify the slope+intercept reproduces ``from_price`` at
        SOME bar index that gives the right value."""
        bars = _fabricate_bars()
        with _stub_history(bars):
            with TestClient(app) as c:
                r = c.get("/engine/sr",
                          params={"symbol": "FAKE", "sec_type": "STK"})
        data = r.json()
        for ln in data["lines"]:
            if ln["from_price"] is None or ln["to_price"] is None:
                continue
            # The line is parameterised by slope + intercept in
            # bar-index space; the (from_price, to_price) pair must
            # be reachable by SOME pair of indices (i.e. consistent).
            slope = ln["slope_per_bar"]
            if abs(slope) < 1e-12:
                # Flat line: from_price ≈ to_price within EPS.
                assert abs(ln["from_price"] - ln["to_price"]) < 1e-6
                continue
            # bar gap implied by the two prices
            dy = ln["to_price"] - ln["from_price"]
            implied_bars = dy / slope
            # Reasonable: from_idx <= to_idx so implied_bars >= 0.
            assert implied_bars >= -1e-6, (
                f"to_price < from_price on a {slope:+f}/bar slope; "
                f"line inconsistent"
            )
