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
