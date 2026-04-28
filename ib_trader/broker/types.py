"""Broker-agnostic types used across all broker implementations.

All broker IDs are strings. All monetary values are Decimal.
All datetimes are UTC.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BrokerCapabilities:
    """Declares what a broker supports. Engine checks these before calling
    broker-specific features."""

    supports_in_place_amend: bool       # IB=True (same order ID), Alpaca=False (PATCH gives new ID)
    supports_extended_hours: bool       # Both=True
    supports_overnight: bool            # IB=True, Alpaca=False
    supports_fractional_shares: bool    # IB=False (most), Alpaca=True (RTH only)
    supports_stop_orders: bool          # Both=True
    commission_free: bool               # IB=False, Alpaca=True
    fill_delivery: str                  # "push" (IB callbacks) or "websocket" (Alpaca TradingStream)
    rate_limit_interval_ms: int         # IB=100, Alpaca=333 (3/sec)
    max_concurrent_connections: int     # IB=32, Alpaca=unlimited


@dataclass
class Instrument:
    """Resolved instrument from a broker.

    Identity for STK: (symbol, exchange, currency). Identity for FUT:
    (root, sec_type, expiry, trading_class, exchange). ``asset_id`` is
    the broker-stable key (IB con_id string, Alpaca UUID).

    New fields (root, sec_type, expiry, trading_class, tick_size,
    con_id) were introduced in Epic 1 Phase 1. Legacy callers that pass
    only the original fields continue to work because every new field
    is optional or defaults sensibly. ``multiplier`` is kept as ``str |
    None`` in the dataclass for schema back-compat, but is always a
    string-encoded Decimal (``"50"``, ``"5"``, ``"1"``); prefer
    ``multiplier_decimal`` at read time.
    """

    asset_id: str              # IB: str(con_id), Alpaca: UUID string
    symbol: str                # Display symbol for STK, or root for FUT (kept for back-compat)
    exchange: str
    currency: str
    multiplier: str | None     # String-encoded Decimal; "50" for ES, "1" or None for STK
    broker: str                # "ib" or "alpaca"
    raw: str                   # JSON of broker-specific response
    # Epic 1 additions — all optional for back-compat:
    root: str | None = None            # "ES", "MES"; None for STK (use ``symbol``)
    sec_type: str = "STK"              # STK / ETF / FUT / OPT
    expiry: str | None = None          # YYYYMMDD (IB-normalized last-trade date) for FUT/OPT
    trading_class: str | None = None   # IB trading-class disambiguator (e.g. "ES" vs "MES")
    tick_size: Decimal | None = None   # Minimum price increment
    con_id: int | None = None          # Broker-stable numeric ID (IB). Same value as asset_id when asset_id is a digit string.

    @property
    def multiplier_decimal(self) -> Decimal:
        """Return multiplier as Decimal; defaults to 1 when unset/None."""
        if self.multiplier is None or self.multiplier == "":
            return Decimal("1")
        return Decimal(self.multiplier)

    @property
    def display_root(self) -> str:
        """Return the root symbol (``root`` if set, else ``symbol``)."""
        return self.root or self.symbol


@dataclass(frozen=True)
class FutureExpiryCandidate:
    """A single contract-month entry returned by ``list_future_expiries``.

    Used by the discovery API route and the ExpiryPicker UI component.
    """

    con_id: int
    root: str
    expiry: str                # YYYYMMDD
    trading_class: str
    exchange: str
    multiplier: Decimal
    tick_size: Decimal


@dataclass
class Snapshot:
    """Market data snapshot: bid, ask, last."""

    bid: Decimal
    ask: Decimal
    last: Decimal


@dataclass
class OrderResult:
    """Result of an order status query."""

    status: str
    qty_filled: Decimal
    avg_fill_price: Decimal | None
    commission: Decimal | None
    error_message: str | None = None
    why_held: str | None = None


@dataclass
class FillResult:
    """Fill notification from the fill stream."""

    broker_order_id: str
    qty_filled: Decimal
    avg_fill_price: Decimal
    commission: Decimal
