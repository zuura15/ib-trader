"""Tick-size snapping for order prices.

Every futures contract has a minimum price increment (``tick_size``).
Orders submitted off-tick are rejected by the exchange, so every price
the engine puts on the wire must land on a tick boundary.

Call-site convention:
- **Engine-computed** prices (mid, walker, profit-taker) are snapped
  ``direction="nearest"`` (or side-aware for walker) before submit.
- **User-supplied** limit prices are validated, not snapped — the engine
  rejects off-tick user input with a clear error. See Epic 1 D5.

Stocks use the convenience wrapper ``snap_for_stk``, which is exactly
``snap_to_tick(price, Decimal("0.01"), "nearest")``.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Literal

Direction = Literal["nearest", "up", "down"]

STK_TICK = Decimal("0.01")


def snap_to_tick(price: Decimal, tick_size: Decimal, direction: Direction = "nearest") -> Decimal:
    """Round ``price`` to the nearest multiple of ``tick_size``.

    Args:
        price: The un-snapped price.
        tick_size: Minimum price increment (must be > 0).
        direction: ``"nearest"`` (ROUND_HALF_UP), ``"up"`` (ceil to tick),
                   ``"down"`` (floor to tick).

    Returns the snapped price with the same scale as ``tick_size``.
    Raises ``ValueError`` for non-positive tick_size or unknown direction.
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive: {tick_size}")
    if direction == "nearest":
        rounding = ROUND_HALF_UP
    elif direction == "up":
        rounding = ROUND_UP
    elif direction == "down":
        rounding = ROUND_DOWN
    else:
        raise ValueError(f"unknown direction: {direction!r}")
    ticks = (price / tick_size).quantize(Decimal("1"), rounding=rounding)
    return (ticks * tick_size).quantize(tick_size)


def is_on_tick(price: Decimal, tick_size: Decimal) -> bool:
    """True when ``price`` is an exact multiple of ``tick_size``."""
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive: {tick_size}")
    return (price / tick_size) % 1 == 0


def snap_for_stk(price: Decimal, direction: Direction = "nearest") -> Decimal:
    """Convenience wrapper for equity tick (``$0.01``)."""
    return snap_to_tick(price, STK_TICK, direction)
