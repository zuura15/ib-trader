"""FastAPI dependency injection.

Provides access to the scoped session factory and repositories
for route handlers via FastAPI's Depends() mechanism.
"""
from sqlalchemy.orm import scoped_session

from ib_trader.data.repository import (
    TradeRepository, OrderRepository, HeartbeatRepository, AlertRepository,
)
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository

# Module-level session factory — set by app.py lifespan on startup.
_session_factory: scoped_session | None = None


def set_session_factory(sf: scoped_session) -> None:
    """Called once at app startup to wire the session factory."""
    global _session_factory
    _session_factory = sf


def get_session_factory() -> scoped_session:
    """FastAPI dependency: returns the scoped session factory."""
    if _session_factory is None:
        raise RuntimeError("Session factory not initialized. Call set_session_factory() first.")
    return _session_factory


def get_trades() -> TradeRepository:
    return TradeRepository(get_session_factory())


def get_orders() -> OrderRepository:
    return OrderRepository(get_session_factory())


def get_heartbeats() -> HeartbeatRepository:
    return HeartbeatRepository(get_session_factory())


def get_alerts() -> AlertRepository:
    return AlertRepository(get_session_factory())


def get_pending_commands() -> PendingCommandRepository:
    return PendingCommandRepository(get_session_factory())
