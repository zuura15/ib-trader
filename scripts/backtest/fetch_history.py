"""One-off fetch of historical bars directly from IB Gateway.

Connects to the Gateway, qualifies the symbol, requests historical
data, writes JSON in the same shape as ``GET /api/history``. Bypasses
the engine — useful for backtests when no `make dev` is running.

Usage:
    uv run python scripts/backtest/fetch_history.py \\
        --symbol MGCM6 --hours 168 --bar-size "3 mins" \\
        --out /tmp/mgc7d.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Make ib_trader importable when run from project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Load .env so IB_HOST / IB_PORT / IB_ACCOUNT_ID are populated when
# this script runs outside of `make dev`.
def _load_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

from ib_trader.config import environment  # noqa: E402
from ib_trader.ib.insync_client import InsyncClient  # noqa: E402


async def fetch(symbol: str, sec_type: str, hours: int, bar_size: str) -> list[dict]:
    host = os.environ.get("IB_HOST", "192.168.4.66")
    port = int(os.environ.get("IB_PORT", "4001"))
    account_id = os.environ.get("IB_ACCOUNT_ID", "")
    # Use a non-conflicting client_id so we don't bump the running
    # engine if any. +50 stays clear of {1, 2} (prod/dev) and any
    # daemon (+1).
    client_id = environment.default_client_id() + 50

    client = InsyncClient(
        host=host, port=port, client_id=client_id, account_id=account_id,
    )
    print(f"connecting to {host}:{port} client_id={client_id}…", file=sys.stderr)
    await client.connect()

    print(f"qualifying {symbol} ({sec_type})…", file=sys.stderr)
    qualified = await client.qualify_contract(symbol, sec_type=sec_type)
    con_id = int(qualified["con_id"])
    contract = client._contract_cache[con_id]

    # IB rejects historical requests > 86400 S in a single call.
    # Chunk into 1-day windows ending at successive day boundaries
    # walking backward from "now". Concatenate + dedup.
    from datetime import datetime, timedelta, timezone
    chunk_seconds = 86400
    chunks_needed = (hours * 3600 + chunk_seconds - 1) // chunk_seconds
    end_dt: datetime | None = None  # None == "now" for the first chunk
    seen_ts: set[str] = set()
    out: list[dict] = []
    for n in range(chunks_needed):
        end_str = ""
        if end_dt is not None:
            # IB format: "YYYYMMDD-HH:MM:SS" in UTC.
            end_str = end_dt.strftime("%Y%m%d-%H:%M:%S")
        print(f"chunk {n + 1}/{chunks_needed}: end={end_str or 'now'}",
              file=sys.stderr)
        bars = await client.req_historical_data_async(
            contract,
            end_date_time=end_str,
            duration_str=f"{chunk_seconds} S",
            bar_size=bar_size,
            what_to_show="BID_ASK",
            use_rth=False,
            format_date=2,
        )
        if not bars:
            break
        # Track the earliest bar in this chunk to set the next
        # chunk's endDateTime.
        earliest = min(b.date for b in bars)
        for bar in bars:
            ts = bar.date
            ts_iso = (ts.isoformat() if hasattr(ts, "isoformat") else str(ts))
            if ts_iso in seen_ts:
                continue
            seen_ts.add(ts_iso)
            out.append({
                "ts": ts_iso,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(getattr(bar, "volume", 0) or 0),
            })
        # Step back: next chunk ends at this chunk's earliest bar.
        if hasattr(earliest, "tzinfo") and earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=timezone.utc)
        end_dt = earliest

    out.sort(key=lambda b: b["ts"])
    await client.disconnect()
    return out


async def main_async() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--sec-type", default="FUT")
    p.add_argument("--hours", type=int, default=168)
    p.add_argument("--bar-size", default="3 mins")
    p.add_argument("--out", default="/tmp/history.json")
    args = p.parse_args()

    bars = await fetch(args.symbol, args.sec_type, args.hours, args.bar_size)
    Path(args.out).write_text(json.dumps({"bars": bars}, indent=2))
    print(f"wrote {len(bars)} bars to {args.out}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
