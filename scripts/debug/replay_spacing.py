"""Replay the inter_touch_spacing helper against the MNQ 00:33 fire.

Anchors on bar timestamps from the audit (Q=2026-05-18T13:12, P/new=2026-05-19T00:30)
and re-derives the bar window the bot actually saw at trade time, so indices line up.
"""
from __future__ import annotations
import json
import urllib.request
from datetime import datetime, timezone

PROD = "http://192.168.4.66:8000"

# From audit id 2945 — the chosen entry line.
LINE_FROM_TIME = "2026-05-18T13:12:00+00:00"
LINE_ANCHOR_B_TIME = "2026-05-19T00:30:00+00:00"
LINE_SLOPE = -1.0400485436893203
LINE_INTERCEPT = 29661.393203883494
LINE_TOUCHES = 8
EVAL_TS = "2026-05-19T00:33:00+00:00"  # the eval bar (new pivot at last_idx-1)


def _fetch_bars(symbol: str, sec_type: str, hours: int):
    url = (
        f"{PROD}/api/history?symbol={symbol}&sec_type={sec_type}"
        f"&hours={hours}&bar_size=3+mins"
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def find_pivot_highs(closes):
    out = []
    for i in range(1, len(closes) - 1):
        if closes[i] > closes[i - 1] and closes[i] > closes[i + 1]:
            out.append(i)
    return out


def main() -> None:
    # Pull more than 24h so we surely cover Q at 13:12 the prior day.
    bars = _fetch_bars("MNQM6", "FUT", hours=36)
    if isinstance(bars, dict):
        bars = bars.get("bars", bars.get("rows", []))
    print(f"fetched {len(bars)} bars; first={bars[0]['ts']} last={bars[-1]['ts']}")
    # Trim to the bot's 24h window ending at eval bar.
    eval_ts = _parse_iso(EVAL_TS)
    # Find the eval bar idx in the full fetch.
    eval_idx = None
    for i, b in enumerate(bars):
        if _parse_iso(b["ts"]) == eval_ts:
            eval_idx = i
            break
    if eval_idx is None:
        # Fallback: nearest.
        eval_idx = min(
            range(len(bars)),
            key=lambda i: abs((_parse_iso(bars[i]["ts"]) - eval_ts).total_seconds()),
        )
    print(f"eval_idx in full fetch: {eval_idx}  ({bars[eval_idx]['ts']})")
    # Bot's window: last 480 bars (24h × 60min / 3min) ending at eval_bar inclusive.
    start = max(0, eval_idx + 1 - 480)
    window = bars[start:eval_idx + 1]
    print(f"window size: {len(window)}  start={window[0]['ts']}  end={window[-1]['ts']}")
    closes = [float(b["close"]) for b in window]
    last_idx = len(closes) - 1
    new_pivot_idx = last_idx - 1
    pivot_highs = find_pivot_highs(closes)
    print(f"last_idx={last_idx}, new_pivot_idx={new_pivot_idx}")
    print(f"pivot_highs count: {len(pivot_highs)}")
    # Find idx for Q and anchor_b (P) in this window.
    q_ts = _parse_iso(LINE_FROM_TIME)
    p_ts = _parse_iso(LINE_ANCHOR_B_TIME)
    from_idx = next((i for i, b in enumerate(window) if _parse_iso(b["ts"]) == q_ts), -1)
    anchor_b_idx = next((i for i, b in enumerate(window) if _parse_iso(b["ts"]) == p_ts), -1)
    print(f"from_idx (Q) = {from_idx}  anchor_b_idx (P) = {anchor_b_idx}  "
          f"new_pivot_idx = {new_pivot_idx}")
    # touch_tol = avg_close * 0.0002
    avg_close = sum(closes) / max(1, len(closes))
    touch_tol = max(1e-6, avg_close * 0.0002)
    print(f"avg_close={avg_close:.2f}  touch_tol={touch_tol:.4f}")
    # Recompute slope/intercept from Q and P (sanity check vs audit).
    if from_idx >= 0 and anchor_b_idx > from_idx:
        slope = (closes[anchor_b_idx] - closes[from_idx]) / (anchor_b_idx - from_idx)
        intercept = closes[anchor_b_idx] - slope * anchor_b_idx
        print(f"recomputed slope={slope:.6f}  intercept={intercept:.4f}")
        print(f"audit          slope={LINE_SLOPE:.6f}  intercept={LINE_INTERCEPT:.4f}")
    else:
        slope, intercept = LINE_SLOPE, LINE_INTERCEPT
        print("WARNING: using audit slope/intercept; could not locate Q or P in window")
    # Iterate side_pivots in [from_idx, new_pivot_idx); collect strict.
    strict = []
    for piv in pivot_highs:
        if piv >= new_pivot_idx:
            break
        if piv < max(0, from_idx):
            continue
        line_at = intercept + slope * piv
        delta = abs(closes[piv] - line_at)
        if delta <= touch_tol:
            strict.append((piv, window[piv]["ts"], closes[piv], line_at, delta))
    print(f"strict touches in [from_idx, new_pivot_idx): {len(strict)}")
    for s in strict:
        print(f"  idx={s[0]} ts={s[1]} close={s[2]:.2f} line={s[3]:.2f} d={s[4]:.3f}")
    # Also list ALL strict in [from_idx, new_pivot_idx] inclusive — sanity.
    full_strict = []
    for piv in pivot_highs:
        if piv > new_pivot_idx:
            break
        if piv < max(0, from_idx):
            continue
        line_at = intercept + slope * piv
        delta = abs(closes[piv] - line_at)
        if delta <= touch_tol:
            full_strict.append((piv, delta))
    print(f"strict touches in [from_idx, new_pivot_idx] inclusive: {len(full_strict)}")
    # Compute filter outcome.
    if len(strict) < 2:
        print("FILTER OUTCOME (current code): insufficient_prior_touches → PASSES (bug)")
    else:
        t_prev = strict[-1][0]
        t_prev_prev = strict[-2][0]
        g_prev = t_prev - t_prev_prev
        g_new = new_pivot_idx - t_prev
        ratio = max(g_prev, g_new) / max(min(g_prev, g_new), 1)
        print(f"g_prev={g_prev} g_new={g_new} ratio={ratio:.3f} max=3.0 (symmetric)")
        print(f"FILTER OUTCOME: {'PASSES' if ratio <= 3.0 else 'REJECTS'}")


if __name__ == "__main__":
    main()
