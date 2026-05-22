"""On-demand V-recovery (and inverted-V) detector for entry gating.

This module is deliberately independent of all existing pivot, fuzzy-line,
regime, and trajectory-curve machinery in the project.

It is intended to be called just-in-time by a bot strategy immediately
before it submits a sell-to-open (or buy-to-open) order.

The only public entry point for v1 is `detect_v_recoveries`.

Design goals (per ADR 020):
- Hard internal 48-hour horizon (never looks further back).
- High recall: even slow / flattish recoveries after a material drop qualify.
- Multiple simultaneous Vs at different scales are reported.
- Rich operator diagnostics: every active trough time + strength token.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# =============================================================================
# Tunables for v1 (easy to adjust after live observation)
# =============================================================================

MAX_HORIZON = timedelta(hours=48)

# Minimum drop from a reference peak to be considered a V at all.
# 1.2 % is a reasonable starting floor for liquid instruments (MES, QQQ, etc.).
MIN_DEPTH_PCT_DEFAULT = 0.012

# Minimum fraction of that drop that must have been recovered by "now"
# before we consider the recovery leg "active" for gating purposes.
MIN_RECOVERY_RATIO_DEFAULT = 0.25

# When multiple troughs are close together we keep only the first one
# in each cluster. This value is in *bars*, not minutes (caller controls bar size).
MIN_TROUGH_SEPARATION_BARS = 25


def _parse_close(bar: dict[str, Any]) -> float:
    """Robustly extract close price as float."""
    val = bar.get("close") or bar.get("c")
    if val is None:
        raise ValueError("bar missing 'close' field")
    return float(val)


def _parse_ts(bar: dict[str, Any]) -> datetime:
    """Parse bar timestamp. Accepts ISO string or datetime."""
    raw = bar.get("ts") or bar.get("timestamp") or bar.get("time")
    if raw is None:
        raise ValueError("bar missing timestamp field ('ts' or 'timestamp')")

    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)

    # ISO-8601 string
    s = str(raw).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _strength_token(depth: float, recovery: float) -> str:
    """
    Encode the two dimensions of strength into a compact token.

    depth   -> letter bucket (percentage drop from its own peak)
    recovery -> integer percentage of that drop already recovered
    """
    if depth >= 0.035:
        letter = "L"
    elif depth >= 0.018:
        letter = "m"
    else:
        letter = "s"
    pct = max(0, min(100, int(round(recovery * 100))))
    return f"{letter}{pct}"


def detect_v_recoveries(
    bars: list[dict[str, Any]],
    *,
    min_depth_pct: float = MIN_DEPTH_PCT_DEFAULT,
    min_recovery_ratio: float = MIN_RECOVERY_RATIO_DEFAULT,
) -> tuple[bool, str]:
    """
    Detect active V-recoveries inside the supplied bar series.

    The detector first truncates the series to the internal 48-hour hard cap
    measured backwards from the last bar. It then searches for every
    (peak, trough) pair that still shows a material recovery at the end of
    the series.

    Returns
    -------
    (has_active_v, diagnostic_line)
        has_active_v is True if at least one qualifying trough exists.
        diagnostic_line is either the empty string or a line of the form:
            "V: 09:15(s38), 11:42(m61), 14:03(L29)"
        Times are HH:mm of the trough bar (UTC or whatever tz the bars carry).

    The detector is intentionally high-recall and accepts slow / flattish
    recoveries. The only two conditions for a trough to appear are:
      1. The drop from its reference peak was >= min_depth_pct, and
      2. The price at the last bar has recovered >= min_recovery_ratio of that drop.
    """
    if len(bars) < 6:
        return False, ""

    # --- 1. Parse + apply 48 h hard cap (internal, not caller-controlled) ---
    parsed: list[tuple[datetime, float]] = []
    for b in bars:
        try:
            ts = _parse_ts(b)
            close = _parse_close(b)
            parsed.append((ts, close))
        except Exception:
            continue  # skip malformed bars silently (robust for live use)

    if len(parsed) < 6:
        return False, ""

    last_ts = parsed[-1][0]
    cutoff = last_ts - MAX_HORIZON
    capped = [(ts, c) for ts, c in parsed if ts >= cutoff]

    if len(capped) < 6:
        return False, ""

    n = len(capped)

    # --- 2. Find candidate troughs (high-recall, multi-scale) ---
    # For every bar i (except the very last few) we consider it a potential
    # trough. We look back a generous but bounded window for the highest
    # price before it. This naturally discovers Vs of many different lengths.
    candidates: list[tuple[datetime, str, int]] = []

    # Look back up to ~20 h worth of bars for the peak (gives good multi-scale
    # coverage while staying O(n * 400) which is trivial for an on-demand call).
    MAX_PEAK_LOOKBACK = 400

    for i in range(3, n - 2):
        lookback = min(MAX_PEAK_LOOKBACK, i)
        peak_idx = max(range(i - lookback, i), key=lambda j: capped[j][1])
        peak_price = capped[peak_idx][1]
        trough_price = capped[i][1]

        if peak_price <= 0 or trough_price >= peak_price:
            continue

        depth = (peak_price - trough_price) / peak_price
        if depth < min_depth_pct:
            continue

        # Recovery measured from THIS trough all the way to the final bar
        current_price = capped[-1][1]
        denom = peak_price - trough_price
        recovery = (current_price - trough_price) / denom if denom > 0 else 0.0

        if recovery >= min_recovery_ratio:
            token = _strength_token(depth, recovery)
            candidates.append((capped[i][0], token, i))

    if not candidates:
        return False, ""

    # --- 3. Deduplicate nearby troughs (keep the earliest in each cluster) ---
    # This prevents the diagnostic from being spammed by 8 micro-troughs
    # inside a 15-minute choppy bottom.
    candidates.sort(key=lambda x: x[2])  # by bar index
    kept: list[tuple[datetime, str]] = []
    last_idx = -10_000

    for ts, token, idx in candidates:
        if idx - last_idx >= MIN_TROUGH_SEPARATION_BARS:
            kept.append((ts, token))
            last_idx = idx

    if not kept:
        return False, ""

    # --- 4. Build the diagnostic line (oldest trough first) ---
    kept.sort(key=lambda x: x[0])
    parts = []
    for ts, token in kept:
        hhmm = ts.strftime("%H:%M")
        parts.append(f"{hhmm}({token})")

    diagnostic = "V: " + ", ".join(parts)
    return True, diagnostic


# -----------------------------------------------------------------------------
# Symmetric helper for long entries (inverted V / recovery from a top)
# Will be implemented in the same style once the short-side behaviour is
# validated. For now the stub makes the import site future-proof.
# -----------------------------------------------------------------------------

def detect_inverted_v_recoveries(
    bars: list[dict[str, Any]],
    *,
    min_depth_pct: float = MIN_DEPTH_PCT_DEFAULT,
    min_recovery_ratio: float = MIN_RECOVERY_RATIO_DEFAULT,
) -> tuple[bool, str]:
    """
    Placeholder for the inverted-V (recovery from a sharp top) detector.
    Same contract as detect_v_recoveries but for "do not go long here".
    Implementation will be added after the short-side version has bake time.
    """
    # For the initial CR we return "no inverted V" so existing call sites
    # that want to be defensive can already import the name.
    return False, ""
