"""Fetch 12 months of 3-min TRADES bars for MGC, MES, MNQ.

Strategy: walk back through the active-month contracts that were
front during each 2-3 month period of the year, fetch ~90 days of
data from each one's expiry-week working backwards. Per-contract
JSON files land in /tmp/long_history_bidask/ — resume-safe (skip files
that already exist and look complete).

Designed to run unattended for 1–3 hours via systemd-inhibit. IB
Gateway must be reachable on the configured host/port.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ib_trader.config import environment  # noqa: E402
from ib_trader.ib.insync_client import InsyncClient  # noqa: E402

OUT_DIR = Path("/tmp/long_history_bidask")
BAR_SIZE = "3 mins"
WHAT_TO_SHOW = "BID_ASK"
CHUNK_SECONDS = 86400
DAYS_PER_CONTRACT = 90       # ample for any one contract's active period
# Per-contract calendar — (symbol_root, YYYYMM, anchor_yyyymmdd_for_end).
# anchor = first day past contract's active period (we walk backwards
# from there); we use mid-expiry-month so IB returns the contract's
# meaningful trading window.
# Order: oldest first so file timestamps reflect fetch order.
CONTRACTS: list[tuple[str, str, str]] = [
    # MGC — monthly futures (Feb Apr Jun Aug Oct Dec). Going back from
    # May 2026 to May 2025 covers M5, Q5, V5, Z5, G6, J6, M6.
    ("MGC", "202506", "20250601"),
    ("MGC", "202508", "20250801"),
    ("MGC", "202510", "20251001"),
    ("MGC", "202512", "20251201"),
    ("MGC", "202602", "20260201"),
    ("MGC", "202604", "20260401"),
    ("MGC", "202606", "20260601"),   # current

    # MES, MNQ — quarterly (Mar Jun Sep Dec).
    ("MES", "202506", "20250620"),
    ("MES", "202509", "20250920"),
    ("MES", "202512", "20251220"),
    ("MES", "202603", "20260320"),
    ("MES", "202606", "20260620"),   # current

    ("MNQ", "202506", "20250620"),
    ("MNQ", "202509", "20250920"),
    ("MNQ", "202512", "20251220"),
    ("MNQ", "202603", "20260320"),
    ("MNQ", "202606", "20260620"),   # current
]


def _out_path(root: str, expiry: str) -> Path:
    return OUT_DIR / f"{root}_{expiry}_3min_bidask.json"


def _looks_complete(p: Path, min_bars: int = 5000) -> bool:
    """A contract file is treated as 'good enough to skip' when it
    holds at least min_bars rows. 5k × 3-min ≈ 10.5 days worth — well
    below DAYS_PER_CONTRACT but enough to catch genuinely-fetched
    files vs partial 0-byte stubs."""
    if not p.exists() or p.stat().st_size < 1000:
        return False
    try:
        data = json.loads(p.read_text())
        bars = data.get("bars", data) if isinstance(data, dict) else data
        return len(bars) >= min_bars
    except Exception:
        return False


def _exchange_for(root: str) -> str:
    """Native exchange per root (no SMART for FUT)."""
    return {
        "MGC": "COMEX",
        "MES": "CME", "MNQ": "CME",
        "ES":  "CME", "NQ":  "CME",
    }.get(root, "CME")


async def _fetch_contract(client, root: str, expiry: str,
                          anchor_yyyymmdd: str) -> None:
    out_path = _out_path(root, expiry)
    if _looks_complete(out_path):
        print(f"[skip] {root} {expiry} already cached "
              f"({out_path.stat().st_size // 1024} KB)", flush=True)
        return

    print(f"[fetch] {root} {expiry} → {out_path.name}", flush=True)
    # Direct ib_async qualify with includeExpired=True so we can pull
    # historical bars from contracts that already settled.
    from ib_async import Future
    exchange = _exchange_for(root)
    fut = Future(
        symbol=root, lastTradeDateOrContractMonth=expiry,
        exchange=exchange, currency="USD",
        includeExpired=True,
    )
    details = await client._InsyncClient__ib.reqContractDetailsAsync(fut)
    if not details:
        print(f"  no contract details for {root} {expiry} on {exchange}",
              flush=True)
        return
    chosen = details[0].contract
    chosen.includeExpired = True
    contract = chosen

    # End anchor: midnight UTC of the anchor date. For the current
    # active contract (anchor in the future), fall back to 'now'.
    try:
        anchor_dt = datetime.strptime(anchor_yyyymmdd, "%Y%m%d").replace(
            tzinfo=timezone.utc,
        )
        if anchor_dt > datetime.now(timezone.utc):
            anchor_dt = None
    except ValueError:
        anchor_dt = None

    end_dt = anchor_dt
    chunks_needed = DAYS_PER_CONTRACT  # 1-day chunks
    bars_out: list[dict] = []
    seen: set[str] = set()
    empty_streak = 0
    for n in range(chunks_needed):
        end_str = end_dt.strftime("%Y%m%d-%H:%M:%S") if end_dt else ""
        try:
            bars = await client.req_historical_data_async(
                contract,
                end_date_time=end_str,
                duration_str=f"{CHUNK_SECONDS} S",
                bar_size=BAR_SIZE,
                what_to_show=WHAT_TO_SHOW,
                use_rth=False,
                format_date=2,
            )
        except Exception as e:
            print(f"  chunk {n+1}/{chunks_needed} FAILED: {e}",
                  flush=True)
            await asyncio.sleep(10)
            empty_streak += 1
            if empty_streak >= 3:
                print(f"  3 failures in a row → stopping {root} {expiry}",
                      flush=True)
                break
            continue
        if not bars:
            empty_streak += 1
            print(f"  chunk {n+1}: empty (streak={empty_streak})",
                  flush=True)
            if empty_streak >= 2:
                print(f"  contract exhausted at chunk {n+1}", flush=True)
                break
            # Step back one day blindly and try again.
            if end_dt is not None:
                from datetime import timedelta as _td
                end_dt = end_dt - _td(days=1)
            continue
        empty_streak = 0
        earliest = min(b.date for b in bars)
        new_count = 0
        for bar in bars:
            ts = bar.date
            ts_iso = (ts.isoformat() if hasattr(ts, "isoformat")
                      else str(ts))
            if ts_iso in seen:
                continue
            seen.add(ts_iso)
            new_count += 1
            bars_out.append({
                "ts": ts_iso,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(getattr(bar, "volume", 0) or 0),
            })
        print(f"  chunk {n+1}/{chunks_needed} end={end_str or 'now'} "
              f"+{new_count} bars (total {len(bars_out)})", flush=True)
        if hasattr(earliest, "tzinfo") and earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=timezone.utc)
        end_dt = earliest
        # Persist incrementally so a mid-run kill keeps progress.
        if n % 5 == 4:
            bars_out.sort(key=lambda b: b["ts"])
            out_path.write_text(json.dumps({
                "symbol": root, "expiry": expiry,
                "bar_size": BAR_SIZE, "what_to_show": WHAT_TO_SHOW,
                "bars": bars_out,
            }))

    bars_out.sort(key=lambda b: b["ts"])
    out_path.write_text(json.dumps({
        "symbol": root, "expiry": expiry,
        "bar_size": BAR_SIZE, "what_to_show": WHAT_TO_SHOW,
        "bars": bars_out,
    }))
    print(f"[done] {root} {expiry}: {len(bars_out)} bars → "
          f"{out_path.name} ({out_path.stat().st_size // 1024} KB)",
          flush=True)


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("IB_HOST", "192.168.4.66")
    port = int(os.environ.get("IB_PORT", "4001"))
    account_id = os.environ.get("IB_ACCOUNT_ID", "")
    client_id = environment.default_client_id() + 50

    client = InsyncClient(
        host=host, port=port, client_id=client_id, account_id=account_id,
    )
    print(f"connecting to {host}:{port} client_id={client_id}…",
          flush=True)
    await client.connect()
    try:
        for root, expiry, anchor in CONTRACTS:
            await _fetch_contract(client, root, expiry, anchor)
    finally:
        await client.disconnect()

    print()
    print("=== summary ===", flush=True)
    for root, expiry, _ in CONTRACTS:
        p = _out_path(root, expiry)
        if p.exists():
            try:
                bars = json.loads(p.read_text())["bars"]
                print(f"  {root} {expiry}: {len(bars):>6} bars  "
                      f"{p.stat().st_size // 1024:>5} KB",
                      flush=True)
            except Exception:
                print(f"  {root} {expiry}: UNREADABLE", flush=True)
        else:
            print(f"  {root} {expiry}: MISSING", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
