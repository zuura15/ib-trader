"""Directional callout signals (#100).

Local slope/curvature classification of recent price action, plus the
compact prompt/parse contract for one-word LLM direction opinions
(Grok / TypeSafe Jev via any OpenAI-compatible endpoint).

Floats throughout are deliberate: these are display-only analytics on
prices (slopes, regressions), never monetary values — the Decimal
tenet applies to money, and nothing here touches an order.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# One-word contract: keeps responses at ~1 token so per-bar calls stay
# cheap and unambiguous to parse.
SYSTEM_PROMPT = (
    "You classify short-term futures price direction for a scalper. "
    "Reply with exactly one word: UP, DOWN, or FLAT."
)

_ANSWER_MAP = {
    "UP": "UP", "DOWN": "DOWN", "FLAT": "FLAT",
    # Common near-synonyms models reach for despite the instruction.
    "SIDEWAYS": "FLAT", "NEUTRAL": "FLAT", "HIGHER": "UP", "LOWER": "DOWN",
}


def _linreg_slope(pts: list[tuple[float, float]]) -> float:
    """Least-squares slope of price vs time, in price units per MINUTE."""
    n = len(pts)
    if n < 2:
        return 0.0
    t0 = pts[0][0]
    xs = [(t - t0) / 60.0 for t, _ in pts]
    ys = [p for _, p in pts]
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def compute_local_direction(
    samples: list[tuple[float, float]],
    *,
    tick_size: float,
    flat_ticks_per_min: float,
    min_samples: int = 30,
) -> dict:
    """Classify recent price action from (epoch_seconds, price) samples.

    Direction comes from the full-window regression slope expressed in
    ticks/minute against the FLAT band. Curvature compares the recent
    half's slope to the older half's: steepening in the trend direction
    is ACCEL, flattening/rolling over is DECEL.
    """
    if len(samples) < min_samples or tick_size <= 0:
        return {"status": "warmup", "dir": "FLAT",
                "slope_ticks_per_min": 0.0, "accel": "STEADY",
                "samples": len(samples)}
    tpm = _linreg_slope(samples) / tick_size
    if tpm > flat_ticks_per_min:
        direction = "UP"
    elif tpm < -flat_ticks_per_min:
        direction = "DOWN"
    else:
        direction = "FLAT"
    half = len(samples) // 2
    d = (_linreg_slope(samples[half:]) - _linreg_slope(samples[:half])) / tick_size
    band = max(flat_ticks_per_min * 0.5, 0.1)
    if direction == "UP":
        accel = "ACCEL" if d > band else ("DECEL" if d < -band else "STEADY")
    elif direction == "DOWN":
        accel = "ACCEL" if d < -band else ("DECEL" if d > band else "STEADY")
    else:
        accel = "STEADY"
    return {"status": "ok", "dir": direction,
            "slope_ticks_per_min": round(tpm, 3), "accel": accel,
            "samples": len(samples)}


def bucket_closes(
    samples: list[tuple[float, float]],
    *,
    bucket_seconds: int = 180,
    max_buckets: int = 40,
    include_partial: bool = False,
) -> list[float]:
    """Downsample tick samples to per-bucket closes (last price wins).

    The in-progress bucket is excluded by default so consecutive LLM
    calls see stable history rather than a half-formed bar.
    """
    buckets: dict[int, float] = {}
    for t, p in samples:
        buckets[int(t // bucket_seconds)] = p
    keys = sorted(buckets)
    if not include_partial and keys and samples:
        cur = int(samples[-1][0] // bucket_seconds)
        keys = [k for k in keys if k != cur]
    return [buckets[k] for k in keys][-max_buckets:]


def build_llm_prompt(
    symbol: str, closes: list[float], last: float, *, bar_seconds: int = 180,
) -> tuple[str, str]:
    """Return (system, user) — user is a bare CSV to keep tokens minimal."""
    # .10g not :g — default 6 sig figs truncates NQ/YM-scale prices
    # (23456.25 -> "23456.2"), silently corrupting the series.
    csv = ",".join(f"{c:.10g}" for c in closes)
    mins = bar_seconds // 60
    user = f"{symbol} {mins}m closes oldest first:{csv} last:{last:.10g}"
    return SYSTEM_PROMPT, user


def parse_llm_answer(text: str) -> str | None:
    """Extract UP/DOWN/FLAT from a model reply; None when unparseable."""
    if not text:
        return None
    word = "".join(ch for ch in text.strip().split()[0] if ch.isalpha()).upper()
    return _ANSWER_MAP.get(word)


# TypeSafe Jev is not a chat model: it answers typed "Choice" questions
# with a probability distribution (POST /v1/systemone). The criteria
# mirror the one-word contract so all three readouts stay comparable.
_JEV_CRITERIA = {
    "UP": "Price is trending higher and likely to keep rising short-term",
    "DOWN": "Price is trending lower and likely to keep falling short-term",
    "FLAT": "No clear short-term trend; price is ranging or directionless",
}


def build_jev_state(
    symbol: str, closes: list[float], last: float, *, bar_seconds: int = 180,
) -> dict:
    """Structured state for a Jev direction question (named fields per docs)."""
    return {
        "symbol": symbol,
        "bar_minutes": bar_seconds // 60,
        "closes_oldest_first": list(closes),
        "last_price": last,
    }


async def query_typesafe_jev(
    base_url: str, api_key: str, model: str,
    state: dict, *, timeout: float = 8.0,
) -> dict:
    """One Choice judgment against TypeSafe's System One endpoint.

    Returns {"choice", "probabilities", "confidence", "model"}. The key
    travels only in the Authorization header — callers must never log it.
    """
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{base_url.rstrip('/')}/v1/systemone",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "state": state,
                "model": model,
                "questions": {
                    "direction": {
                        "type": "choice",
                        "instructions": (
                            "Short-term price direction of this futures "
                            "series over the next few minutes, for a scalper"),
                        "criteria": _JEV_CRITERIA,
                    },
                },
            },
        )
        r.raise_for_status()
        data = r.json()
        ans = data["answers"]["direction"]
        return {
            "choice": str(ans.get("choice") or ""),
            "probabilities": ans.get("probabilities") or {},
            "confidence": ans.get("confidence"),
            "model": str(data.get("model") or model),
        }


async def query_openai_compatible(
    base_url: str, api_key: str, model: str,
    system: str, user: str, *, timeout: float = 8.0,
) -> str:
    """One chat completion against any OpenAI-compatible endpoint.

    The key travels only in the Authorization header — callers must
    never log it (httpx errors carry the URL, not headers).
    """
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "max_tokens": 6,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        r.raise_for_status()
        data = r.json()
        return str(data["choices"][0]["message"]["content"] or "")
