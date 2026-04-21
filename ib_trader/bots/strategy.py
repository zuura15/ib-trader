"""Strategy Protocol and core types for the bot runtime.

This module defines the typed contracts between strategies and the runtime:
- Strategy Protocol: what a strategy must implement
- Events: typed data the runtime delivers to strategies
- Actions: typed instructions strategies return to the runtime

The position lifecycle is owned by ``BotState`` in ``ib_trader.bots.fsm``.
Strategies read ``StrategyContext.fsm_state`` to route event handling;
they never write lifecycle transitions — ``FSM.dispatch()`` is the sole
writer. The runtime dispatches FSM events around each strategy action
(PLACE_ENTRY_ORDER, ENTRY_FILLED, PLACE_EXIT_ORDER, EXIT_CANCELLED…).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ib_trader.bots.lifecycle import BotState


# ---------------------------------------------------------------------------
# Enum vocabulary
# ---------------------------------------------------------------------------

class QuoteField(str, enum.Enum):
    """Which price field on a QuoteUpdate to use for stop/trail decisions.

    Config key ``exit_price`` in strategies/*.yaml selects one of these.
    """
    BID = "bid"
    ASK = "ask"
    LAST = "last"


class ExitType(str, enum.Enum):
    """Reason an exit policy fired. Used in LogSignal.payload["exit_type"]
    and for telemetry / audit filters."""
    HARD_STOP_LOSS = "HARD_STOP_LOSS"
    TRAILING_STOP  = "TRAILING_STOP"
    TIME_STOP      = "TIME_STOP"
    TAKE_PROFIT    = "TAKE_PROFIT"   # reserved for future policies
    FORCE_EXIT     = "FORCE_EXIT"    # operator-triggered manual override


class LogEventType(str, enum.Enum):
    """Categorizes LogSignal entries. Written as-is to the bot_events
    audit table's event_type column; the frontend groups by this field.

    Includes both strategy-emitted values (BAR, SIGNAL, …) and
    runner-emitted bot-lifecycle values (STARTED, STOPPED, …) because
    they share the audit table."""
    BAR        = "BAR"
    SIGNAL     = "SIGNAL"
    SKIP       = "SKIP"
    FILL       = "FILL"
    STATE      = "STATE"
    EXIT_CHECK = "EXIT_CHECK"
    CLOSED     = "CLOSED"
    RISK       = "RISK"
    ERROR      = "ERROR"
    ORDER      = "ORDER"
    MANUAL_ENTRY_ONLY = "MANUAL_ENTRY_ONLY"
    # Bot lifecycle (runner-emitted)
    STARTED    = "STARTED"
    STOPPED    = "STOPPED"


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
    """Submit an order to the engine.

    `origin` tags the producer of the action so downstream middleware can
    distinguish automatic strategy entries from exits and manual overrides:

      - "strategy"        — strategy.on_event() emitted this as part of its
                            normal signal logic (auto-entry, auto-exit flagged
                            by strategy intent). The default.
      - "exit"            — trailing stop, hard stop, time stop, or any
                            strategy-driven exit branch.
      - "manual_override" — operator-forced action (e.g. FORCE_BUY) routed
                            outside the normal signal path.

    `ManualEntryMiddleware` uses this to block only `origin=="strategy"`
    BUYs on test bots, so exits and manual overrides are untouched.
    """
    symbol: str
    side: str  # "BUY" or "SELL"
    qty: Decimal
    order_type: str  # See Strategy enum in ib_trader.repl.commands for the full set.
    price: Decimal | None = None  # required for limit orders
    params: dict = field(default_factory=dict)  # profit_target, stop_loss, tif, etc.
    origin: str = "strategy"  # "strategy" | "exit" | "manual_override"


@dataclass(frozen=True)
class CancelOrder:
    """Cancel an existing order."""
    trade_serial: int
    origin: str = "strategy"


@dataclass(frozen=True)
class UpdateState:
    """Persist strategy-defined state."""
    state: dict


@dataclass(frozen=True)
class LogSignal:
    """Log a signal/event to the bot events audit trail."""
    event_type: LogEventType | str  # prefer LogEventType; str accepted for back-compat
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
        state: Strategy-defined persistent state dict (entry_price, hwm,
               trail_activated, current_stop, qty, entry_time, etc.).
               Lifecycle transitions are NOT stored here.
        fsm_state: Current FSM state (AWAITING_ENTRY_TRIGGER /
                   ENTRY_ORDER_PLACED / AWAITING_EXIT_TRIGGER /
                   EXIT_ORDER_PLACED). Strategies route event handling on
                   this; they never write it — FSM.dispatch() is the
                   sole writer.
        bot_id: Unique bot instance identifier.
        config: Strategy configuration from YAML.
    """
    state: dict
    fsm_state: BotState
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

    def build_exit_actions(self, ctx: StrategyContext, exit_type: "ExitType",
                           detail: str) -> list["Action"]:
        """Build the actions that close the current position.

        Called both by the strategy's own exit policies (trailing stop,
        hard stop, time stop) and by the runtime's force-sell path so that
        an operator-triggered exit produces bit-identical orders to an
        organic one — they differ only in ``exit_type``.
        """
        ...
