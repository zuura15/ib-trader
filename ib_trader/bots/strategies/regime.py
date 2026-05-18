"""Local regime classification for chart_signal entries.

Reads recent bars via pandas-ta indicators (ADX, ATR, Donchian) and
returns a single ``RegimeReading`` that the entry path uses to gate
direction (up-trend blocks SHORTs / down-trend blocks LONGs) and to
apply flat-regime gates (Donchian extreme + ATR amplitude).

Stage placement in ``_on_bar``: invoked AFTER pivot detection (which
is the cheapest gate) and BEFORE 3-touch line search (which is the
most expensive step). Saves the line-search work on regime rejection
and keeps regime decisions out of the per-line filter loop.

Thresholds follow standard Wilder-ADX convention:
  - ADX < ranging_threshold (default 20)  → flat
  - ADX > trending_threshold (default 25) → trending; direction = sign(+DI − −DI)
  - in between                            → uncertain (treated as flat-conservative)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegimeReading:
    regime: str  # "up" | "down" | "flat" | "uncertain" | "insufficient"
    sufficient_bars: bool
    n_bars: int
    adx: float | None = None
    dmp: float | None = None  # +DI
    dmn: float | None = None  # −DI
    atr: float | None = None
    dcu: float | None = None  # Donchian upper
    dcl: float | None = None  # Donchian lower

    @property
    def is_trending(self) -> bool:
        return self.regime in ("up", "down")

    @property
    def is_flat_conservative(self) -> bool:
        # ``uncertain`` and ``insufficient`` both fall through to the
        # flat-regime gates (extreme + amplitude) — when we don't know,
        # we demand the stricter conditions.
        return self.regime in ("flat", "uncertain", "insufficient")

    def to_audit_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "regime": self.regime,
            "n_bars": self.n_bars,
        }
        for k in ("adx", "dmp", "dmn", "atr", "dcu", "dcl"):
            v = getattr(self, k)
            if v is not None:
                out[k] = round(v, 4)
        return out


def compute_regime(
    bars: list[dict],
    *,
    adx_period: int = 14,
    atr_period: int = 14,
    donchian_period: int = 20,
    trending_threshold: float = 25.0,
    ranging_threshold: float = 20.0,
) -> RegimeReading:
    """Compute ADX/ATR/Donchian over the bar window and classify.

    ``bars`` is a list of dicts with at least ``high``, ``low``,
    ``close`` keys (same shape as ``/engine/history`` output).
    Returns ``RegimeReading.regime == "insufficient"`` when warm-up
    bars are missing or any indicator value is NaN.
    """
    # Need enough bars for the longest warmup + 1 valid output bar.
    needed = max(adx_period, atr_period, donchian_period) + 2
    if len(bars) < needed:
        return RegimeReading(
            regime="insufficient", sufficient_bars=False, n_bars=len(bars),
        )

    # Defer the pandas import to call time — keeps strategy load cheap.
    import pandas as pd
    import pandas_ta  # noqa: F401 — registers the ``df.ta`` accessor

    try:
        df = pd.DataFrame([
            {"high": float(b.get("high", 0.0) or 0.0),
             "low": float(b.get("low", 0.0) or 0.0),
             "close": float(b.get("close", 0.0) or 0.0)}
            for b in bars
        ])
    except (TypeError, ValueError):
        return RegimeReading(
            regime="insufficient", sufficient_bars=False, n_bars=len(bars),
        )

    adx_df = df.ta.adx(length=adx_period)
    atr_s = df.ta.atr(length=atr_period)
    don_df = df.ta.donchian(
        upper_length=donchian_period, lower_length=donchian_period,
    )
    if adx_df is None or atr_s is None or don_df is None:
        return RegimeReading(
            regime="insufficient", sufficient_bars=False, n_bars=len(bars),
        )

    adx_col = f"ADX_{adx_period}"
    dmp_col = f"DMP_{adx_period}"
    dmn_col = f"DMN_{adx_period}"
    dcu_col = f"DCU_{donchian_period}_{donchian_period}"
    dcl_col = f"DCL_{donchian_period}_{donchian_period}"
    try:
        adx = float(adx_df[adx_col].iloc[-1])
        dmp = float(adx_df[dmp_col].iloc[-1])
        dmn = float(adx_df[dmn_col].iloc[-1])
        atr = float(atr_s.iloc[-1])
        dcu = float(don_df[dcu_col].iloc[-1])
        dcl = float(don_df[dcl_col].iloc[-1])
    except (KeyError, IndexError, ValueError):
        return RegimeReading(
            regime="insufficient", sufficient_bars=False, n_bars=len(bars),
        )

    if any(math.isnan(v) for v in (adx, dmp, dmn, atr, dcu, dcl)):
        return RegimeReading(
            regime="insufficient", sufficient_bars=False, n_bars=len(bars),
            adx=adx if not math.isnan(adx) else None,
            dmp=dmp if not math.isnan(dmp) else None,
            dmn=dmn if not math.isnan(dmn) else None,
            atr=atr if not math.isnan(atr) else None,
            dcu=dcu if not math.isnan(dcu) else None,
            dcl=dcl if not math.isnan(dcl) else None,
        )

    if adx < ranging_threshold:
        regime = "flat"
    elif adx > trending_threshold:
        regime = "up" if dmp > dmn else "down"
    else:
        regime = "uncertain"

    return RegimeReading(
        regime=regime, sufficient_bars=True, n_bars=len(bars),
        adx=adx, dmp=dmp, dmn=dmn, atr=atr, dcu=dcu, dcl=dcl,
    )


def passes_amplitude(
    reading: RegimeReading,
    *,
    cost_floor: float,
    typical_bars_in_trade: float = 5.0,
    min_edge_mult: float = 2.0,
) -> bool:
    """Expected-swing ≥ ``min_edge_mult × cost_floor``.

    ``expected_swing = ATR × typical_bars_in_trade``. Returns True
    when there's enough room for the typical trade to clear costs.
    ``cost_floor`` is round-trip commission + 2× tol × contract_mult
    in price units of the bar (already multiplier-applied by caller).
    """
    if reading.atr is None or reading.atr <= 0:
        return False
    expected_swing = reading.atr * typical_bars_in_trade
    return expected_swing >= min_edge_mult * cost_floor


def at_donchian_extreme(
    reading: RegimeReading,
    *,
    price: float,
    side: str,   # "short" → upper extreme; "long" → lower extreme
    tol: float,
) -> bool:
    """Current price within ``tol`` of the regime-side Donchian band.

    SHORT requires the current pivot to be near the N-bar high
    (price ≥ DCU − tol); LONG requires near the N-bar low
    (price ≤ DCL + tol). When DCU/DCL are unavailable, returns False
    (caller treats as fail-closed).
    """
    if side == "short":
        if reading.dcu is None:
            return False
        return price >= reading.dcu - tol
    if reading.dcl is None:
        return False
    return price <= reading.dcl + tol


@dataclass(frozen=True)
class VState:
    """V-recovery / inverted-V detection state. Display-only —
    does NOT gate entries (yet).

    Three triggers, all symmetric for V (down then up) vs
    inverted-V (up then down):

      - BOS  (option 1, "break of structure"): price has closed
        past the pre-impulse extreme on the recovery side. Latest
        but most defensible "yes, the V is real" signal.
      - A    (impulse-reversal): retrace_pct ≥ retrace_pct_threshold
        within retrace_max_bars of the impulse extreme. Catches
        sharp V-bottoms early.
      - B    (impulse-exhaustion): impulse extreme has held for
        ≥ exhaustion_bars without a new extreme. Catches "L"
        patterns where the recovery is shallow but the down-leg
        has clearly stopped.

    ``detected = trigger_a OR trigger_b OR bos``.
    """
    detected: bool
    direction: str | None  # "v_up" | "v_down" | None
    impulse_extreme_idx: int | None
    impulse_extreme_time: str | None  # ISO timestamp of the bar
    impulse_extreme_price: float | None
    impulse_magnitude: float | None
    impulse_atr_mult: float | None
    trigger_a_fired: bool
    trigger_b_fired: bool
    bos_confirmed: bool
    retrace_pct: float | None
    bars_since_extreme: int | None

    def to_audit_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "detected": self.detected,
            "direction": self.direction,
            "trigger_a_fired": self.trigger_a_fired,
            "trigger_b_fired": self.trigger_b_fired,
            "bos_confirmed": self.bos_confirmed,
        }
        for k in ("impulse_extreme_idx", "impulse_extreme_time",
                  "impulse_extreme_price",
                  "impulse_magnitude", "impulse_atr_mult",
                  "retrace_pct", "bars_since_extreme"):
            v = getattr(self, k)
            if v is not None:
                out[k] = round(v, 4) if isinstance(v, float) else v
        return out


def _v_state_none(reason: str | None = None) -> VState:
    return VState(
        detected=False,
        direction=None,
        impulse_extreme_idx=None,
        impulse_extreme_time=None,
        impulse_extreme_price=None,
        impulse_magnitude=None,
        impulse_atr_mult=None,
        trigger_a_fired=False,
        trigger_b_fired=False,
        bos_confirmed=False,
        retrace_pct=None,
        bars_since_extreme=None,
    )


def detect_v_state(
    bars: list[dict],
    *,
    atr: float,
    impulse_lookback_bars: int = 20,
    impulse_atr_mult: float = 5.0,
    retrace_pct_threshold: float = 0.30,
    retrace_max_bars: int = 12,
    exhaustion_bars: int = 10,
) -> VState:
    """Detect V-recovery / inverted-V patterns.

    Looks at the last ``impulse_lookback_bars`` of closes. Finds
    both directional candidates (lowest close = V-bottom candidate,
    highest close = inverted-V-top candidate). For each, checks if
    the move FROM the pre-extreme opposite point TO the extreme is
    ≥ ``impulse_atr_mult × atr`` — that's a real impulse.

    When two impulses are present (rare in 20 bars), the one with
    the LARGER magnitude wins. Returns the V state for that
    direction with all three trigger flags evaluated.

    Display-only — does NOT gate entries.
    """
    if not bars or atr is None or atr <= 0:
        return _v_state_none()
    if len(bars) < max(3, impulse_lookback_bars // 2):
        return _v_state_none()

    try:
        closes = [float(b.get("close", 0.0) or 0.0) for b in bars]
    except (TypeError, ValueError):
        return _v_state_none()

    last_idx = len(closes) - 1
    window_start = max(0, last_idx - impulse_lookback_bars + 1)
    window_closes = closes[window_start:last_idx + 1]
    if len(window_closes) < 3:
        return _v_state_none()

    # Find the lowest and highest closes in the window.
    low_offset = window_closes.index(min(window_closes))
    high_offset = window_closes.index(max(window_closes))
    low_idx = window_start + low_offset
    high_idx = window_start + high_offset
    window_low = closes[low_idx]
    window_high = closes[high_idx]

    # Candidate V (down-impulse then recovery):
    #   pre-impulse high (somewhere before low_idx) → low at low_idx.
    # Require the low to be strictly BEFORE last_idx so there's at
    # least one bar of post-extreme action to evaluate retrace /
    # exhaustion against. If the low is the most recent bar, the
    # impulse is still in progress and we can't yet say a V is
    # forming.
    v_up_magnitude = None
    v_up_pre_idx = None
    if low_idx > window_start and low_idx < last_idx:
        pre_window = closes[window_start:low_idx]
        pre_high = max(pre_window)
        v_up_pre_idx = window_start + pre_window.index(pre_high)
        v_up_magnitude = pre_high - window_low

    # Candidate inverted-V (up-impulse then drop):
    #   pre-impulse low (somewhere before high_idx) → high at high_idx.
    # Same "extreme not at last_idx" requirement.
    v_down_magnitude = None
    v_down_pre_idx = None
    if high_idx > window_start and high_idx < last_idx:
        pre_window = closes[window_start:high_idx]
        pre_low = min(pre_window)
        v_down_pre_idx = window_start + pre_window.index(pre_low)
        v_down_magnitude = window_high - pre_low

    # Pick the stronger candidate (largest impulse in ATR units).
    chosen_direction: str | None = None
    impulse_extreme_idx: int | None = None
    impulse_extreme_price: float | None = None
    impulse_magnitude: float | None = None
    pre_impulse_extreme: float | None = None  # opposite-side extreme

    up_mult = (v_up_magnitude / atr) if v_up_magnitude else 0
    down_mult = (v_down_magnitude / atr) if v_down_magnitude else 0

    if (up_mult >= impulse_atr_mult and up_mult >= down_mult
            and v_up_magnitude is not None):
        chosen_direction = "v_up"
        impulse_extreme_idx = low_idx
        impulse_extreme_price = window_low
        impulse_magnitude = v_up_magnitude
        pre_impulse_extreme = closes[v_up_pre_idx] if v_up_pre_idx is not None else None
    elif (down_mult >= impulse_atr_mult and down_mult > up_mult
            and v_down_magnitude is not None):
        chosen_direction = "v_down"
        impulse_extreme_idx = high_idx
        impulse_extreme_price = window_high
        impulse_magnitude = v_down_magnitude
        pre_impulse_extreme = closes[v_down_pre_idx] if v_down_pre_idx is not None else None

    if chosen_direction is None or impulse_extreme_idx is None:
        return _v_state_none()

    impulse_mag_val: float = impulse_magnitude or 0.0
    impulse_extreme_price_val: float = impulse_extreme_price or 0.0

    bars_since_extreme = last_idx - impulse_extreme_idx

    # Trigger A — retrace.
    post = closes[impulse_extreme_idx:last_idx + 1]
    if chosen_direction == "v_up":
        retrace = max(post) - impulse_extreme_price_val
    else:
        retrace = impulse_extreme_price_val - min(post)
    retrace_pct = (
        retrace / impulse_mag_val if impulse_mag_val > 0 else 0.0
    )
    trigger_a_fired = (
        retrace_pct >= retrace_pct_threshold
        and bars_since_extreme <= retrace_max_bars
        and bars_since_extreme >= 1
    )

    # Trigger B — exhaustion.
    trigger_b_fired = bars_since_extreme >= exhaustion_bars

    # BOS — break of structure past the pre-impulse extreme.
    bos_confirmed = False
    if pre_impulse_extreme is not None and impulse_extreme_idx < last_idx:
        post_recovery = closes[impulse_extreme_idx + 1:last_idx + 1]
        if post_recovery:
            if chosen_direction == "v_up":
                bos_confirmed = max(post_recovery) > pre_impulse_extreme
            else:
                bos_confirmed = min(post_recovery) < pre_impulse_extreme

    detected = trigger_a_fired or trigger_b_fired or bos_confirmed

    # Look up the bar timestamp at the impulse extreme. Bars from
    # /engine/history carry the timestamp under "ts" (ISO string);
    # event.window-style bars use "timestamp_utc" (datetime). Try
    # both and stringify whatever's there.
    extreme_bar = bars[impulse_extreme_idx]
    raw_ts = extreme_bar.get("ts") or extreme_bar.get("timestamp_utc")
    if raw_ts is None:
        extreme_time = None
    elif hasattr(raw_ts, "isoformat"):
        extreme_time = raw_ts.isoformat()
    else:
        extreme_time = str(raw_ts)

    return VState(
        detected=detected,
        direction=chosen_direction,
        impulse_extreme_idx=impulse_extreme_idx,
        impulse_extreme_time=extreme_time,
        impulse_extreme_price=impulse_extreme_price_val,
        impulse_magnitude=impulse_mag_val,
        impulse_atr_mult=(
            up_mult if chosen_direction == "v_up" else down_mult
        ),
        trigger_a_fired=trigger_a_fired,
        trigger_b_fired=trigger_b_fired,
        bos_confirmed=bos_confirmed,
        retrace_pct=retrace_pct,
        bars_since_extreme=bars_since_extreme,
    )
