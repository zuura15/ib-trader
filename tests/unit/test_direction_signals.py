"""Unit tests for the directional callout signals (#100)."""
import pytest

from ib_trader.signals.direction import (
    bucket_closes, build_jev_state, build_llm_prompt, compute_local_direction,
    parse_llm_answer, query_typesafe_jev,
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

    def test_jev_state_shape(self):
        state = build_jev_state("NQZ6", [100.25, 100.5], 100.75, bar_seconds=180)
        assert state == {
            "symbol": "NQZ6",
            "bar_minutes": 3,
            "closes_oldest_first": [100.25, 100.5],
            "last_price": 100.75,
        }

    def test_parse_variants(self):
        assert parse_llm_answer("UP") == "UP"
        assert parse_llm_answer(" down\n") == "DOWN"
        assert parse_llm_answer("Flat.") == "FLAT"
        assert parse_llm_answer("Sideways") == "FLAT"
        assert parse_llm_answer("I think higher") is None  # first word rule
        assert parse_llm_answer("") is None
        assert parse_llm_answer("42") is None

    def test_prompt_keeps_full_price_precision(self):
        # 6-sig-fig :g used to truncate NQ-scale prices to 23456.2.
        _, user = build_llm_prompt("NQZ6", [23456.25], 23456.75)
        assert "23456.25" in user
        assert "last:23456.75" in user


class TestJevQuery:
    @pytest.mark.asyncio
    async def test_choice_request_and_response(self, monkeypatch):
        import httpx
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "model": "jev-1.13.0",
                "answers": {"direction": {
                    "type": "choice", "choice": "DOWN",
                    "probabilities": {"UP": 0.1, "DOWN": 0.8, "FLAT": 0.1},
                    "confidence": 0.65,
                }},
                "usage": {"input_tokens": 100, "output_tokens": 5},
            })

        real_client = httpx.AsyncClient

        def patched(**kw):
            return real_client(transport=httpx.MockTransport(handler), **kw)

        monkeypatch.setattr(httpx, "AsyncClient", patched)
        state = build_jev_state("NQZ6", [100.0, 99.5], 99.25)
        out = await query_typesafe_jev(
            "https://api.typesafe.ai", "sekret", "jev-latest", state)
        assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
        assert seen["auth"] == "Bearer sekret"
        assert seen["body"]["model"] == "jev-latest"
        assert seen["body"]["state"]["symbol"] == "NQZ6"
        q = seen["body"]["questions"]["direction"]
        assert q["type"] == "choice"
        assert set(q["criteria"]) == {"UP", "DOWN", "FLAT"}
        assert out["choice"] == "DOWN"
        assert out["probabilities"]["DOWN"] == 0.8
        assert out["confidence"] == 0.65
        assert out["model"] == "jev-1.13.0"
