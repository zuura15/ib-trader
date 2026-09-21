"""Unit tests for the directional callout signals (#100)."""
from ib_trader.signals.direction import (
    bucket_closes, build_llm_prompt, compute_local_direction, parse_llm_answer,
)

TICK = 0.25


def _series(fn, n=120, dt=2.0):
    return [(i * dt, fn(i)) for i in range(n)]


class TestLocalDirection:
    def test_rising_series_is_up(self):
        out = compute_local_direction(
            _series(lambda i: 100 + 0.05 * i),
            tick_size=TICK, flat_ticks_per_min=0.5,
        )
        assert out["status"] == "ok"
        assert out["dir"] == "UP"
        assert out["slope_ticks_per_min"] > 0.5

    def test_falling_series_is_down(self):
        out = compute_local_direction(
            _series(lambda i: 100 - 0.05 * i),
            tick_size=TICK, flat_ticks_per_min=0.5,
        )
        assert out["dir"] == "DOWN"

    def test_flat_series_is_flat(self):
        out = compute_local_direction(
            _series(lambda i: 100 + (0.01 if i % 2 else -0.01)),
            tick_size=TICK, flat_ticks_per_min=0.5,
        )
        assert out["dir"] == "FLAT"
        assert out["accel"] == "STEADY"

    def test_accelerating_uptrend(self):
        out = compute_local_direction(
            _series(lambda i: 100 + 0.001 * i * i),
            tick_size=TICK, flat_ticks_per_min=0.5,
        )
        assert out["dir"] == "UP"
        assert out["accel"] == "ACCEL"

    def test_decelerating_downtrend(self):
        # Steep early drop that flattens out late.
        out = compute_local_direction(
            _series(lambda i: 100 - 0.002 * (240 * i - i * i) / 2),
            tick_size=TICK, flat_ticks_per_min=0.5,
        )
        assert out["dir"] == "DOWN"
        assert out["accel"] == "DECEL"

    def test_warmup_below_min_samples(self):
        out = compute_local_direction(
            _series(lambda i: 100 + i, n=10),
            tick_size=TICK, flat_ticks_per_min=0.5,
        )
        assert out["status"] == "warmup"


class TestBucketing:
    def test_last_price_wins_and_partial_excluded(self):
        samples = [(0.0, 1.0), (100.0, 2.0), (180.0, 3.0), (359.0, 4.0),
                   (360.0, 5.0)]  # third bucket is in-progress
        closes = bucket_closes(samples, bucket_seconds=180)
        assert closes == [2.0, 4.0]

    def test_max_buckets_cap(self):
        samples = [(i * 180.0, float(i)) for i in range(60)]
        closes = bucket_closes(samples, bucket_seconds=180, max_buckets=40)
        assert len(closes) == 40
        assert closes[-1] == 58.0  # 59 is the partial bucket


class TestPromptAndParse:
    def test_prompt_is_compact(self):
        closes = [23456.25 + i * 0.25 for i in range(40)]
        system, user = build_llm_prompt("NQZ6", closes, closes[-1] + 0.25)
        assert len(user) < 600
        assert "NQZ6" in user
        assert "\n" not in user

    def test_parse_variants(self):
        assert parse_llm_answer("UP") == "UP"
        assert parse_llm_answer(" down\n") == "DOWN"
        assert parse_llm_answer("Flat.") == "FLAT"
        assert parse_llm_answer("Sideways") == "FLAT"
        assert parse_llm_answer("I think higher") is None  # first word rule
        assert parse_llm_answer("") is None
        assert parse_llm_answer("42") is None
