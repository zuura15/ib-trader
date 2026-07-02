"""Persistent 24h execution accumulator for the chart P&L rollup.

IB's ``reqExecutions`` only returns executions from the *current* Gateway
session — after the Gateway's daily auto-restart it no longer returns
pre-restart fills (observed floor ~9 h, well short of 24 h). The rollup
recomputed from that snapshot each sweep therefore undercounts the 24 h
window after any restart, and silently drops whole contracts (e.g. an
overnight MNQ batch).

Fix: accumulate every execution we have ever seen into a durable store
keyed by ``exec_id``, pruned to the trailing window. Once a fill has been
observed in any sweep it stays in the window even after IB forgets it, so
the rollup and markers are computed from the union, not just the latest
snapshot. We keep IB's authoritative per-fill ``realized_pnl`` (refreshed
when its commission report finally pairs) rather than recomputing P&L.

Pure / JSON-safe so it can be unit-tested without IB or Redis. The engine
loop owns load → merge → prune → save (single writer, no races).

Limitation: this can only preserve fills observed while the engine was
running. Trades executed while the engine was down AND already aged past
IB's reqExecutions floor are unrecoverable from the Gateway (they live
only in IB's account history / Flex).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

# ib_async returns some execution times shifted +host-offset into the
# future (the IB "implied timezone" bug). A fill can never be in the
# future, so at INGEST (when we first see it) any time past now is that
# shift — roll it back by the host offset and freeze the corrected value.
# Doing it here (frozen at first-seen) rather than against the live clock
# is what makes it durable: a shifted time drifts into the past as the
# day advances, which a live-clock check would then stop correcting.
_FUTURE_GRACE = timedelta(minutes=2)
from typing import Any

# JSON-safe record fields kept per execution.
_NUM_FIELDS = ("price", "shares", "commission")


def _iso(ts: Any) -> str | None:
    if isinstance(ts, datetime):
        d = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    if isinstance(ts, str) and ts:
        return ts
    return None


def _to_record(ex: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a req_recent_executions dict to a JSON-safe record."""
    exec_id = ex.get("exec_id") or ""
    sym = ex.get("local_symbol") or ""
    ts = _iso(ex.get("exec_time"))
    if not exec_id or not sym or ts is None:
        return None
    rp = ex.get("realized_pnl")
    rec: dict[str, Any] = {
        "local_symbol": sym,
        "symbol": ex.get("symbol") or "",
        "sec_type": ex.get("sec_type") or "",
        "side": ex.get("side") or "",
        "exec_id": exec_id,
        "perm_id": int(ex.get("perm_id") or 0),
        "exec_time": ts,
        "realized_pnl": None if rp is None else str(rp),
    }
    for f in _NUM_FIELDS:
        v = ex.get(f)
        rec[f] = None if v is None else str(v)
    return rec


def _deshift_time(iso: str | None, future_cutoff: datetime,
                  host_offset: timedelta) -> str | None:
    """Roll an ib_async future-shifted execution time back by the host
    offset. A fill can't be in the future, so a time past ``future_cutoff``
    is the +offset shift; corrected once here and frozen. Left untouched
    (past times) otherwise."""
    if not iso:
        return iso
    try:
        d = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    if d > future_cutoff:
        return (d + host_offset).isoformat()
    return iso


def merge_executions(
    stored: dict[str, dict],
    fresh: list[dict],
    now: datetime,
    window_hours: float = 24.0,
) -> dict[str, dict]:
    """Merge ``fresh`` executions into ``stored`` (by exec_id) and prune.

    ``stored`` is the prior JSON-safe map ``{exec_id: record}`` (e.g. from
    Redis). Returns a NEW map; the input is not mutated. Upsert rules:
      - unseen exec_id → inserted;
      - seen exec_id whose stored ``realized_pnl`` is still None → refreshed
        from the fresh row once its commission report has paired (so a fill
        first seen unpaired gets its real P&L on a later sweep);
      - otherwise the stored record is kept (IB's realized P&L is final).
    Entries older than ``window_hours`` before ``now`` are dropped.
    """
    now_utc = now.astimezone(timezone.utc)
    host_offset = now.utcoffset() or timedelta(0)
    future_cutoff = now_utc + _FUTURE_GRACE

    out = dict(stored)
    for ex in fresh:
        rec = _to_record(ex)
        if rec is None:
            continue
        prev = out.get(rec["exec_id"])
        if prev is None:
            # Correct the ib_async +offset time shift at first-seen, using
            # ingest time as the "can't be future" reference, and freeze it.
            rec["exec_time"] = _deshift_time(
                rec.get("exec_time"), future_cutoff, host_offset,
            )
            out[rec["exec_id"]] = rec
        elif prev.get("realized_pnl") is None and rec.get("realized_pnl") is not None:
            # Pairing completed since we first saw it — take the paired
            # realized P&L (and the commission that arrived with it).
            prev["realized_pnl"] = rec["realized_pnl"]
            if rec.get("commission") is not None:
                prev["commission"] = rec["commission"]
        # else: keep prev unchanged.

    cutoff = (now.astimezone(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    return {
        eid: r for eid, r in out.items()
        if (r.get("exec_time") or "") >= cutoff
    }


def records_to_execs(stored: dict[str, dict]) -> list[dict]:
    """Convert the JSON-safe store back to exec dicts for the compute fns.

    ``exec_time`` → tz-aware datetime, numeric/realized fields → Decimal
    (None preserved). Shape matches ``req_recent_executions`` so it feeds
    both ``compute_pnl_rollup`` and ``compute_exec_markers`` unchanged.
    """
    execs: list[dict] = []
    for r in stored.values():
        ts_raw = r.get("exec_time")
        try:
            ts = datetime.fromisoformat(ts_raw) if ts_raw else None
        except (ValueError, TypeError):
            ts = None
        if ts is None:
            continue
        rp = r.get("realized_pnl")
        ex: dict[str, Any] = {
            "local_symbol": r.get("local_symbol") or "",
            "symbol": r.get("symbol") or "",
            "sec_type": r.get("sec_type") or "",
            "side": r.get("side") or "",
            "exec_id": r.get("exec_id") or "",
            "perm_id": int(r.get("perm_id") or 0),
            "exec_time": ts,
            "realized_pnl": None if rp is None else Decimal(str(rp)),
        }
        for f in _NUM_FIELDS:
            v = r.get(f)
            ex[f] = None if v is None else Decimal(str(v))
        execs.append(ex)
    return execs
