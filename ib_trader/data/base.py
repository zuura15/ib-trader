"""Abstract repository interfaces for all data access.

All concrete implementations live in repository.py.
No raw SQL is written outside the data/ layer.
Sessions are never exposed outside repository methods.
"""
from abc import ABC, abstractmethod
from decimal import Decimal

from ib_trader.data.models import (
    TradeGroup, RepriceEvent, Contract,
    SystemHeartbeat, SystemAlert, PendingCommand,
    TradeStatus, PendingCommandStatus,
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


class RepriceEventRepositoryBase(ABC):
    """Abstract interface for reprice event persistence."""

    @abstractmethod
    def create(self, evt: RepriceEvent) -> RepriceEvent:
        """Persist a new reprice event and return it."""
        ...

    @abstractmethod
    def get_for_correlation_id(self, correlation_id: str) -> list[RepriceEvent]:
        """Return all reprice events for a correlation ID, ordered by step number."""
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


class PendingCommandRepositoryBase(ABC):
    """Abstract interface for the pending_commands queue."""

    @abstractmethod
    def insert(self, cmd: PendingCommand) -> PendingCommand:
        """Insert a new pending command and return it."""
        ...

    @abstractmethod
    def get(self, cmd_id: str) -> PendingCommand | None:
        """Return the command with the given ID, or None."""
        ...

    @abstractmethod
    def get_pending(self) -> list[PendingCommand]:
        """Return all commands with status PENDING, ordered by submitted_at."""
        ...

    @abstractmethod
    def get_by_status(self, status: PendingCommandStatus) -> list[PendingCommand]:
        """Return all commands with the given status."""
        ...

    @abstractmethod
    def update_status(self, cmd_id: str, status: PendingCommandStatus) -> None:
        """Update the status and started_at timestamp of a command."""
        ...

    @abstractmethod
    def complete(self, cmd_id: str, status: PendingCommandStatus,
                 output: str | None = None, error: str | None = None) -> None:
        """Mark a command as completed (SUCCESS or FAILURE) with output/error."""
        ...

    @abstractmethod
    def get_by_source(self, source: str, limit: int = 50) -> list[PendingCommand]:
        """Return recent commands from a given source, newest first."""
        ...
