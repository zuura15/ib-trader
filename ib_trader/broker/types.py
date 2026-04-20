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
    """Resolved instrument from a broker. Replaces the IB-specific con_id pattern."""

    asset_id: str           # IB: str(con_id), Alpaca: UUID string
    symbol: str
    exchange: str
    currency: str
    multiplier: str | None  # For futures
    broker: str             # "ib" or "alpaca"
    raw: str                # JSON of broker-specific response


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
