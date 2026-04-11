"""Strategy Protocol and core types for the bot runtime.

This module defines the typed contracts between strategies and the runtime:
- Strategy Protocol: what a strategy must implement
- Events: typed data the runtime delivers to strategies
- Actions: typed instructions strategies return to the runtime
- PositionState: the state machine governing position lifecycle

No ib_trader runtime imports allowed here — this is a pure type module.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Position State Machine
# ---------------------------------------------------------------------------

class PositionState(str, enum.Enum):
    """State machine for a single position lifecycle.

    FLAT ──(signal)──→ ENTERING ──(fill)──→ OPEN ──(stop)──→ EXITING ──(fill)──→ FLAT
      ▲                    │                                      │
      └── (timeout/reject) ┘                                      │
      └──────────────────────────────────────────────────────────┘
    """
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"


# ---------------------------------------------------------------------------
# Subscriptions — what data a strategy needs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Subscription:
    """Declares a data subscription the strategy needs from the runtime.

    Attributes:
        type: "bars", "ticks", "options_chain", "news", "timer"
        symbols: Ticker symbols to subscribe to.
        params: Type-specific parameters (e.g. bar_seconds, lookback).
    """
    type: str
    symbols: list[str]
    params: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Strategy Manifest — self-description of a strategy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyManifest:
    """Metadata a strategy publishes so the runtime can wire it up.

    Attributes:
        name: Unique strategy identifier.
        subscriptions: Data feeds the strategy needs.
        capabilities: Runtime services used (execution, state_store).
        state_schema: Description of strategy-defined state shape.
        version: Strategy version for compatibility tracking.
    """
    name: str
    subscriptions: list[Subscription]
    capabilities: list[str] = field(default_factory=list)
    state_schema: dict = field(default_factory=dict)
    version: str = "1.0"


# ---------------------------------------------------------------------------
# Events — typed data the runtime delivers to strategies
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BarCompleted:
    """A target-size bar has closed."""
    symbol: str
    bar: dict  # {timestamp_utc, open, high, low, close, volume}
    window: list[dict]  # last N completed bars for feature computation
    bar_count: int  # total bars completed since bot start


@dataclass(frozen=True)
class QuoteUpdate:
    """A streaming quote update (for exit monitoring)."""
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class OrderFilled:
    """An order has been filled (fully or partially)."""
    trade_serial: int
    symbol: str
    side: str  # "BUY" or "SELL"
    fill_price: Decimal
    qty: Decimal
    commission: Decimal
    ib_order_id: str


@dataclass(frozen=True)
class OrderRejected:
    """An order was rejected or timed out."""
    trade_serial: int | None
    symbol: str
    reason: str
    command_id: str


@dataclass(frozen=True)
class PositionUpdate:
    """Position state changed (from IB reconciliation)."""
    symbol: str
    qty: Decimal
    avg_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class TimerFired:
    """A scheduled timer has fired."""
    name: str
    scheduled_at: datetime


# Union of all event types
MarketEvent = BarCompleted | QuoteUpdate | OrderFilled | OrderRejected | PositionUpdate | TimerFired


# ---------------------------------------------------------------------------
# Actions — typed instructions strategies return to the runtime
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlaceOrder:
    """Submit an order to the engine."""
    symbol: str
    side: str  # "BUY" or "SELL"
    qty: Decimal
    order_type: str  # "mid", "market", "limit"
    price: Decimal | None = None  # required for limit orders
    params: dict = field(default_factory=dict)  # profit_target, stop_loss, tif, etc.


@dataclass(frozen=True)
class CancelOrder:
    """Cancel an existing order."""
    trade_serial: int


@dataclass(frozen=True)
class UpdateState:
    """Persist strategy-defined state."""
    state: dict


@dataclass(frozen=True)
class LogSignal:
    """Log a signal/event to the bot events audit trail."""
    event_type: str  # BAR, EVAL, SKIP, SIGNAL, ORDER, FILL, STATE, EXIT_CHECK, CLOSED, RISK, ERROR
    message: str
    payload: dict = field(default_factory=dict)
    trade_serial: int | None = None


# Union of all action types
Action = PlaceOrder | CancelOrder | UpdateState | LogSignal


# ---------------------------------------------------------------------------
# Strategy Context — injected services available to strategies
# ---------------------------------------------------------------------------

@dataclass
class StrategyContext:
    """Services and state available to strategies during event processing.

    Attributes:
        state: Strategy-defined persistent state dict.
        position_state: Current position lifecycle state.
        bot_id: Unique bot instance identifier.
        config: Strategy configuration from YAML.
    """
    state: dict
    position_state: PositionState
    bot_id: str
    config: dict


# ---------------------------------------------------------------------------
# Strategy Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Strategy(Protocol):
    """Interface that all trading strategies must implement.

    The runtime reads `manifest` to wire up data subscriptions and
    capabilities, then delivers typed events via `on_event()`.
    Strategies return a list of Actions for the middleware pipeline.
    """
    manifest: StrategyManifest

    async def on_start(self, ctx: StrategyContext) -> list[Action]:
        """Called when the bot starts or restarts after crash."""
        ...

    async def on_event(self, event: MarketEvent, ctx: StrategyContext) -> list[Action]:
        """Process a market event and return actions."""
        ...

    async def on_stop(self, ctx: StrategyContext) -> list[Action]:
        """Called when the bot is stopped. Return cleanup actions."""
        ...
