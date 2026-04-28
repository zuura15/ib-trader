"""Pure pricing functions for the IB Trader engine.

All functions are pure — no IB calls, no DB access.
All inputs and outputs are Decimal.
Fully unit-testable in isolation.

Every price-producing function takes a required keyword-only ``tick_size``
argument (see Epic 1 D5). Computed prices are snapped to the tick grid
using banker's rounding (``ROUND_HALF_EVEN``) — preserving historical
$0.01 STK behaviour exactly. User-supplied limits are validated
separately via ``engine.ticks.is_on_tick`` (rejected rather than snapped).
"""
from decimal import ROUND_DOWN, Decimal


_SHARE_PLACES = Decimal("1")


def _snap(value: Decimal, tick_size: Decimal) -> Decimal:
    """Round ``value`` to the nearest multiple of ``tick_size`` using
    banker's rounding (``ROUND_HALF_EVEN``, Decimal's default).

    Kept module-private — callers use the tick-aware helpers below, or
    the separate ``engine.ticks`` module for user-input validation.
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive: {tick_size}")
    ticks = (value / tick_size).quantize(Decimal("1"))
    return (ticks * tick_size).quantize(tick_size)


def calc_mid(bid: Decimal, ask: Decimal, *, tick_size: Decimal) -> Decimal:
    """Return (bid + ask) / 2, snapped to the contract's tick grid.

    Args:
        bid: Current best bid price.
        ask: Current best ask price.
        tick_size: Contract minimum price increment (e.g. ``0.01`` for
                   most STK, ``0.25`` for ES).
    """
    return _snap((bid + ask) / Decimal("2"), tick_size)


def calc_step_price(
    bid: Decimal,
    ask: Decimal,
    step: int,
    total_steps: int,
    side: str = "BUY",
    *,
    tick_size: Decimal,
) -> Decimal:
    """Return the price for a reprice step (1-indexed).

    BUY  orders: start at mid, walk toward ask.
        Formula: mid + (step / total_steps) * (ask - mid)
        step=total_steps → ask exactly.

    SELL orders: start at mid, walk toward bid.
        Formula: mid + (step / total_steps) * (bid - mid)
        step=total_steps → bid exactly.

    Raises ValueError if total_steps is zero.
    """
    if total_steps == 0:
        raise ValueError("total_steps must be greater than zero")
    mid = calc_mid(bid, ask, tick_size=tick_size)
    target = ask if side == "BUY" else bid
    price = mid + (Decimal(step) / Decimal(total_steps)) * (target - mid)
    return _snap(price, tick_size)


def calc_profit_taker_price(
    avg_fill_price: Decimal,
    qty_filled: Decimal,
    profit_amount: Decimal,
    *,
    tick_size: Decimal,
    multiplier: Decimal = Decimal("1"),
) -> Decimal:
    """Return profit taker price for a BUY entry.

    Formula: ``avg_fill_price + profit_amount / (qty_filled * multiplier)``.

    ``multiplier`` accounts for contract-size: for STK it's 1 (per-share
    profit). For futures ES (multiplier=50) a $500 profit on 1 contract
    means price needs to rise by only $10, not $500.

    Raises ValueError if qty_filled or multiplier is zero.
    """
    if qty_filled == 0:
        raise ValueError("qty_filled must be greater than zero")
    if multiplier == 0:
        raise ValueError("multiplier must be greater than zero")
    per_unit = profit_amount / (qty_filled * multiplier)
    return _snap(avg_fill_price + per_unit, tick_size)


def calc_profit_taker_price_short(
    avg_fill_price: Decimal,
    qty_filled: Decimal,
    profit_amount: Decimal,
    *,
    tick_size: Decimal,
    multiplier: Decimal = Decimal("1"),
) -> Decimal:
    """Return profit taker price for a SELL (short) entry.

    Formula: ``avg_fill_price - profit_amount / (qty_filled * multiplier)``.
    """
    if qty_filled == 0:
        raise ValueError("qty_filled must be greater than zero")
    if multiplier == 0:
        raise ValueError("multiplier must be greater than zero")
    per_unit = profit_amount / (qty_filled * multiplier)
    return _snap(avg_fill_price - per_unit, tick_size)


def calc_shares_from_dollars(
    dollars: Decimal,
    price: Decimal,
    max_shares: int,
) -> Decimal:
    """Return share count for a given dollar notional, capped at max_shares.

    Formula: ``floor(dollars / price)``, capped at max_shares.

    For futures sizing see ``notional_value`` — share-count semantics
    don't apply to contracts.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    raw = (dollars / price).quantize(_SHARE_PLACES, rounding=ROUND_DOWN)
    return min(raw, Decimal(max_shares))


def notional_value(
    qty: Decimal,
    price: Decimal,
    multiplier: Decimal = Decimal("1"),
) -> Decimal:
    """Gross notional = ``qty * price * multiplier``.

    Used for position-size displays and the post-fill notional log line.
    This is **not** a margin figure — true margin comes from IB (see
    Epic 1 scope tenet). ``multiplier=1`` for STK preserves the existing
    ``qty * price`` display.
    """
    return qty * price * multiplier
