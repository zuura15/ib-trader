"""Abstract repository interfaces for all data access.

All concrete implementations live in repository.py.
No raw SQL is written outside the data/ layer.
Sessions are never exposed outside repository methods.
"""
from abc import ABC, abstractmethod
from decimal import Decimal

from ib_trader.data.models import (
    TradeGroup, Order, RepriceEvent, Contract,
    SystemHeartbeat, SystemAlert, TradeStatus, OrderStatus
)


class TradeRepositoryBase(ABC):
    """Abstract interface for trade group persistence."""

    @abstractmethod
    def create(self, trade: TradeGroup) -> TradeGroup:
        """Persist a new trade group and return it."""
        ...

    @abstractmethod
    def get_by_serial(self, serial: int) -> TradeGroup | None:
        """Return the trade group with the given serial number, or None."""
        ...

    @abstractmethod
    def get_open(self) -> list[TradeGroup]:
        """Return all trade groups with status OPEN."""
        ...

    @abstractmethod
    def get_all(self) -> list[TradeGroup]:
        """Return all trade groups ordered by serial number descending."""
        ...

    @abstractmethod
    def update_status(self, trade_id: str, status: TradeStatus) -> None:
        """Update the status of a trade group."""
        ...

    @abstractmethod
    def update_pnl(self, trade_id: str, pnl: Decimal, commission: Decimal) -> None:
        """Update realized P&L and total commission for a trade group."""
        ...

    @abstractmethod
    def next_serial_number(self) -> int:
        """Return the lowest unused integer serial number in range 0–999."""
        ...


class OrderRepositoryBase(ABC):
    """Abstract interface for order leg persistence."""

    @abstractmethod
    def create(self, order: Order) -> Order:
        """Persist a new order and return it."""
        ...

    @abstractmethod
    def get_by_id(self, order_id: str) -> Order | None:
        """Return the order with the given UUID, or None."""
        ...

    @abstractmethod
    def get_by_ib_order_id(self, ib_order_id: str) -> Order | None:
        """Return the order with the given IB order ID, or None."""
        ...

    @abstractmethod
    def get_open_for_trade(self, trade_id: str) -> list[Order]:
        """Return all open orders for a trade group."""
        ...

    @abstractmethod
    def get_for_trade(self, trade_id: str) -> list[Order]:
        """Return all orders for a trade group regardless of status."""
        ...

    @abstractmethod
    def get_all_open(self) -> list[Order]:
        """Return all orders that are currently open (any non-terminal status)."""
        ...

    @abstractmethod
    def get_in_states(self, states: list[OrderStatus]) -> list[Order]:
        """Return all orders in any of the given statuses."""
        ...

    @abstractmethod
    def update_status(self, order_id: str, status: OrderStatus) -> None:
        """Update the status of an order."""
        ...

    @abstractmethod
    def update_fill(self, order_id: str, qty_filled: Decimal,
                    avg_price: Decimal, commission: Decimal) -> None:
        """Record fill details on an order."""
        ...

    @abstractmethod
    def update_ib_order_id(self, order_id: str, ib_order_id: str) -> None:
        """Write the IB-assigned order ID to the order record."""
        ...

    @abstractmethod
    def update_amended(self, order_id: str, new_price: Decimal) -> None:
        """Record the latest amendment price and timestamp."""
        ...

    @abstractmethod
    def set_raw_response(self, order_id: str, raw: str) -> None:
        """Store the raw IB API response JSON string."""
        ...


class RepriceEventRepositoryBase(ABC):
    """Abstract interface for reprice event persistence."""

    @abstractmethod
    def create(self, evt: RepriceEvent) -> RepriceEvent:
        """Persist a new reprice event and return it."""
        ...

    @abstractmethod
    def get_for_order(self, order_id: str) -> list[RepriceEvent]:
        """Return all reprice events for an order, ordered by step number."""
        ...

    @abstractmethod
    def confirm_amendment(self, event_id: str) -> None:
        """Mark a reprice event as amendment confirmed by IB."""
        ...


class ContractRepositoryBase(ABC):
    """Abstract interface for contract cache persistence."""

    @abstractmethod
    def get(self, symbol: str) -> Contract | None:
        """Return the cached contract for the symbol, or None."""
        ...

    @abstractmethod
    def upsert(self, contract: Contract) -> None:
        """Insert or update the cached contract for the symbol."""
        ...

    @abstractmethod
    def invalidate(self, symbol: str) -> None:
        """Delete the cached contract for the symbol, forcing re-fetch."""
        ...

    @abstractmethod
    def is_fresh(self, symbol: str, ttl_seconds: int) -> bool:
        """Return True if the cached contract is within the TTL window."""
        ...


class HeartbeatRepositoryBase(ABC):
    """Abstract interface for process heartbeat persistence."""

    @abstractmethod
    def upsert(self, process: str, pid: int) -> None:
        """Insert or update the heartbeat record for a process."""
        ...

    @abstractmethod
    def get(self, process: str) -> SystemHeartbeat | None:
        """Return the heartbeat record for a process, or None."""
        ...

    @abstractmethod
    def delete(self, process: str) -> None:
        """Delete the heartbeat record for a process (on clean exit)."""
        ...


class AlertRepositoryBase(ABC):
    """Abstract interface for system alert persistence."""

    @abstractmethod
    def create(self, alert: SystemAlert) -> SystemAlert:
        """Persist a new system alert and return it."""
        ...

    @abstractmethod
    def get_open(self) -> list[SystemAlert]:
        """Return all unresolved system alerts."""
        ...

    @abstractmethod
    def resolve(self, alert_id: str) -> None:
        """Mark an alert as resolved with the current UTC timestamp."""
        ...
