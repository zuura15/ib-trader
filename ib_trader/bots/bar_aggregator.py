"""Aggregate raw 5-second IB bars into target-size bars.

The engine streams 5-second bars into the market_bars SQLite table.
This module reads those rows, builds OHLCV bars at the target interval
(e.g. 3 minutes = 180 seconds), and maintains a ring buffer of completed
bars for feature computation.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class BarAggregator:
    """Aggregates 5-second bars into target-size OHLCV bars.

    Args:
        target_seconds: Target bar size in seconds (e.g. 180 for 3-min).
        lookback_bars: Number of completed bars to keep in the ring buffer.
    """

    def __init__(self, target_seconds: int, lookback_bars: int) -> None:
        self.target_seconds = target_seconds
        self.lookback_bars = lookback_bars
        self._completed: deque[dict] = deque(maxlen=lookback_bars)
        self._current: dict | None = None  # partial bar being built
        self._ticks_in_current: int = 0
        self._last_seen_ts: datetime | None = None
        self._total_bar_count: int = 0

    def add_bars(self, raw_bars: list[dict]) -> list[dict]:
        """Ingest new 5-second bars and return any newly completed target bars.

        Args:
            raw_bars: List of dicts with keys: timestamp_utc, open, high, low,
                      close, volume. Must be sorted ascending by timestamp.

        Returns:
            List of completed target-size bar dicts (usually 0 or 1).
        """
        completed = []
        for bar in raw_bars:
            ts = bar["timestamp_utc"]
            if self._last_seen_ts is not None and ts <= self._last_seen_ts:
                continue  # dedup on restart
            self._last_seen_ts = ts

            if self._current is None:
                self._current = {
                    "timestamp_utc": ts,
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                }
                self._ticks_in_current = 1
            else:
                self._current["high"] = max(self._current["high"], bar["high"])
                self._current["low"] = min(self._current["low"], bar["low"])
                self._current["close"] = bar["close"]
                self._current["volume"] += bar["volume"]
                self._ticks_in_current += 1

            # Check if we've accumulated enough 5-sec ticks for a full bar
            if self._ticks_in_current * 5 >= self.target_seconds:
                self._completed.append(self._current)
                self._total_bar_count += 1
                completed.append(self._current)
                self._current = None
                self._ticks_in_current = 0

        return completed

    def get_bar_window(self) -> list[dict] | None:
        """Return the last lookback_bars completed bars, or None if insufficient.

        Returns:
            List of bar dicts sorted ascending by timestamp, or None.
        """
        if len(self._completed) < self.lookback_bars:
            return None
        return list(self._completed)

    @property
    def bar_count(self) -> int:
        """Total number of completed target bars since startup."""
        return self._total_bar_count

    @property
    def buffered_bars(self) -> int:
        """Number of bars currently in the ring buffer."""
        return len(self._completed)

    @property
    def has_partial(self) -> bool:
        """True if there's a partial bar being built."""
        return self._current is not None

    def to_state_dict(self) -> dict:
        """Serialize aggregator state for persistence."""
        return {
            "completed": list(self._completed),
            "current": self._current,
            "ticks_in_current": self._ticks_in_current,
            "last_seen_ts": self._last_seen_ts.isoformat() if self._last_seen_ts else None,
            "total_bar_count": self._total_bar_count,
            "target_seconds": self.target_seconds,
            "lookback_bars": self.lookback_bars,
        }

    @classmethod
    def from_state_dict(cls, data: dict) -> BarAggregator:
        """Reconstruct aggregator from persisted state."""
        agg = cls(
            target_seconds=data["target_seconds"],
            lookback_bars=data["lookback_bars"],
        )
        for bar in data.get("completed", []):
            agg._completed.append(bar)
        agg._current = data.get("current")
        agg._ticks_in_current = data.get("ticks_in_current", 0)
        ts_str = data.get("last_seen_ts")
        if ts_str:
            agg._last_seen_ts = datetime.fromisoformat(ts_str)
        agg._total_bar_count = data.get("total_bar_count", 0)
        return agg


def flush_state_to_file(state_dir: Path, bot_id: str, symbol: str, state: dict) -> None:
    """Write aggregator state to a JSON file."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{bot_id}-{symbol}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str))
    tmp.replace(path)  # atomic rename


def load_state_from_file(state_dir: Path, bot_id: str, symbol: str) -> dict | None:
    """Load aggregator state from a JSON file, or None if not found."""
    path = state_dir / f"{bot_id}-{symbol}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning('{"event": "STATE_FILE_CORRUPT", "path": "%s", "error": "%s"}',
                        path, exc)
        return None
