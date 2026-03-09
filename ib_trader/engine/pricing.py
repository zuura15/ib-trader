"""Pure pricing functions for the IB Trader engine.

All functions are pure — no IB calls, no DB access.
All inputs and outputs are Decimal.
Fully unit-testable in isolation.
"""
from decimal import Decimal, ROUND_DOWN


_PRICE_PLACES = Decimal("0.01")
_SHARE_PLACES = Decimal("1")


def calc_mid(bid: Decimal, ask: Decimal) -> Decimal:
    """Return (bid + ask) / 2, rounded to 2 decimal places.

    Args:
        bid: Current best bid price.
        ask: Current best ask price.

    Returns:
        Mid price rounded to 2 decimal places.
    """
    return ((bid + ask) / Decimal("2")).quantize(_PRICE_PLACES)


def calc_step_price(
    bid: Decimal, ask: Decimal, step: int, total_steps: int, side: str = "BUY"
) -> Decimal:
    """Return the price for a reprice step (1-indexed).

    BUY  orders: start at mid, walk toward ask.
        Formula: mid + (step / total_steps) * (ask - mid)
        step=total_steps → ask exactly.

    SELL orders: start at mid, walk toward bid.
        Formula: mid + (step / total_steps) * (bid - mid)
        step=total_steps → bid exactly.

    Args:
        bid: Current best bid price.
        ask: Current best ask price.
        step: Current step number (1-indexed).
        total_steps: Total number of reprice steps.
        side: "BUY" or "SELL".

    Returns:
        Limit price for this step, rounded to 2 decimal places.

    Raises:
        ValueError: If total_steps is zero.
    """
    if total_steps == 0:
        raise ValueError("total_steps must be greater than zero")
    mid = calc_mid(bid, ask)
    target = ask if side == "BUY" else bid
    price = mid + (Decimal(step) / Decimal(total_steps)) * (target - mid)
    return price.quantize(_PRICE_PLACES)


def calc_profit_taker_price(
    avg_fill_price: Decimal,
    qty_filled: Decimal,
    profit_amount: Decimal,
) -> Decimal:
    """Return profit taker price for a BUY entry.

    Formula: avg_fill_price + (profit_amount / qty_filled)

    For SELL entries the caller must negate the result:
        profit_price = avg_fill_price - (profit_amount / qty_filled)

    This function computes only the per-share profit add-on.

    Args:
        avg_fill_price: Average fill price of the entry order.
        qty_filled: Quantity filled on the entry order.
        profit_amount: Total dollar profit target.

    Returns:
        Profit taker price rounded to 2 decimal places.

    Raises:
        ValueError: If qty_filled is zero.
    """
    if qty_filled == 0:
        raise ValueError("qty_filled must be greater than zero")
    return (avg_fill_price + (profit_amount / qty_filled)).quantize(_PRICE_PLACES)


def calc_profit_taker_price_short(
    avg_fill_price: Decimal,
    qty_filled: Decimal,
    profit_amount: Decimal,
) -> Decimal:
    """Return profit taker price for a SELL (short) entry.

    Formula: avg_fill_price - (profit_amount / qty_filled)

    Args:
        avg_fill_price: Average fill price of the entry order.
        qty_filled: Quantity filled on the entry order.
        profit_amount: Total dollar profit target (positive value).

    Returns:
        Profit taker price (lower than entry) rounded to 2 decimal places.

    Raises:
        ValueError: If qty_filled is zero.
    """
    if qty_filled == 0:
        raise ValueError("qty_filled must be greater than zero")
    return (avg_fill_price - (profit_amount / qty_filled)).quantize(_PRICE_PLACES)


def calc_shares_from_dollars(
    dollars: Decimal,
    price: Decimal,
    max_shares: int,
) -> Decimal:
    """Return share count for a given dollar notional, capped at max_shares.

    Formula: floor(dollars / price), capped at max_shares.

    Args:
        dollars: Dollar notional size.
        price: Mid price at time of calculation.
        max_shares: Maximum allowed shares (from settings).

    Returns:
        Share count as Decimal (whole number).

    Raises:
        ValueError: If price is zero or negative.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    raw = (dollars / price).quantize(_SHARE_PLACES, rounding=ROUND_DOWN)
    return min(raw, Decimal(max_shares))
