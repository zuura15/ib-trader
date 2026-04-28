"""Symbol formatting and futures month-code helpers.

Two canonical output formats exist for a futures contract:

- **Display**: ``"ES Z26"`` — root, space, month letter, 2-digit year.
  Used everywhere in our UI (positions, orders, trades, watchlist, logs).
- **IB-paste**: ``"ESZ6"`` — root, month letter, 1-digit year.
  Matches IB TWS contract-search shorthand for near-decade contracts;
  suitable for clipboard paste into IB's own tools.

Stocks pass through unchanged: display and paste both return the symbol.

All inputs and outputs are strings or plain ints. Functions are pure and
have no project imports; safe to reuse from CLI parsing, TUI, API
serializers, and tests.
"""
from __future__ import annotations


MONTH_CODES: dict[int, str] = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}
CODE_MONTHS: dict[str, int] = {v: k for k, v in MONTH_CODES.items()}


def month_to_code(month: int) -> str:
    """Return the single-letter futures month code for a 1-12 month."""
    if month not in MONTH_CODES:
        raise ValueError(f"month out of range 1-12: {month}")
    return MONTH_CODES[month]


def code_to_month(code: str) -> int:
    """Return the 1-12 month for a single-letter futures month code."""
    upper = code.upper()
    if upper not in CODE_MONTHS:
        raise ValueError(f"unknown futures month code: {code!r}")
    return CODE_MONTHS[upper]


def parse_month_code(token: str) -> tuple[int, int]:
    """Parse a futures month-code token (e.g. ``"Z26"``, ``"Z6"``).

    Returns ``(month_1_to_12, year_2_digit)``. A 1-digit year is accepted
    and widened by assuming the current decade; when ambiguous the caller
    should prefer the 2-digit form.

    Raises ValueError for unknown codes or malformed tokens.
    """
    if not token:
        raise ValueError("empty month-code token")
    letter = token[0].upper()
    rest = token[1:]
    if letter not in CODE_MONTHS:
        raise ValueError(f"unknown futures month code: {token!r}")
    if not rest.isdigit() or not (1 <= len(rest) <= 2):
        raise ValueError(f"malformed month-code year: {token!r}")
    year = int(rest)
    if len(rest) == 1:
        year = _widen_single_digit_year(year)
    return CODE_MONTHS[letter], year


def expiry_to_month_year(expiry: str) -> tuple[int, int]:
    """Split an ``YYYYMM`` or ``YYYYMMDD`` expiry string into (month, YY).

    YY is the 2-digit year. Use this when building a display / paste
    symbol from a stored expiry.
    """
    if len(expiry) not in (6, 8) or not expiry.isdigit():
        raise ValueError(f"expiry must be YYYYMM or YYYYMMDD: {expiry!r}")
    year = int(expiry[:4])
    month = int(expiry[4:6])
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range in expiry: {expiry!r}")
    return month, year % 100


def format_display_symbol(root: str, sec_type: str, expiry: str | None) -> str:
    """Normalized display string shown in every UI pane."""
    if sec_type.upper() != "FUT":
        return root
    if not expiry:
        raise ValueError("FUT display symbol requires an expiry")
    month, yy = expiry_to_month_year(expiry)
    return f"{root} {month_to_code(month)}{yy:02d}"


def format_ib_paste_symbol(root: str, sec_type: str, expiry: str | None) -> str:
    """IB-compatible clipboard shorthand (``ESZ6`` style)."""
    if sec_type.upper() != "FUT":
        return root
    if not expiry:
        raise ValueError("FUT IB-paste symbol requires an expiry")
    month, yy = expiry_to_month_year(expiry)
    return f"{root}{month_to_code(month)}{yy % 10}"


def _widen_single_digit_year(digit: int) -> int:
    """Map a 1-digit year to a 2-digit year in the current decade.

    ``Z6`` → 26 when the current 2-digit year is in the 20s. For a decade
    boundary this picks the nearest upcoming year, since futures contracts
    are never in the distant past.
    """
    from datetime import date
    current_yy = date.today().year % 100
    decade_start = current_yy - (current_yy % 10)
    candidate = decade_start + digit
    if candidate < current_yy - 1:
        candidate += 10
    return candidate
