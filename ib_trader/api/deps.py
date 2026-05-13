"""FastAPI dependency injection.

Provides access to the scoped session factory and repositories
for route handlers via FastAPI's Depends() mechanism.
"""
from sqlalchemy.orm import scoped_session

from ib_trader.data.repository import (
    TradeRepository, HeartbeatRepository, AlertRepository,
)
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repositories.bot_trade_repository import BotTradeRepository

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


def _release_thread_session() -> None:
    """Call ``scoped_session.remove()`` on the module-level factory.

    Repositories use ``scoped_session`` which is thread-local. FastAPI
    runs sync endpoint handlers in a threadpool — each request lands
    on a different worker thread and the factory hands back a fresh
    Session bound to that thread's pool connection. Without an explicit
    remove() the Session stays parked in the thread-local registry and
    the connection it holds is never returned to the pool, so the pool
    exhausts after ~50 requests. ``remove()`` clears the thread-local
    so the connection is checked back in.
    """
    sf = _session_factory
    if sf is not None:
        try:
            sf.remove()
        except Exception:
            # remove() can fail if no session was opened on this thread
            # (e.g. dependency injected but endpoint short-circuited);
            # safe to ignore.
            pass


def get_trades():
    try:
        yield TradeRepository(get_session_factory())
    finally:
        _release_thread_session()


def get_heartbeats():
    try:
        yield HeartbeatRepository(get_session_factory())
    finally:
        _release_thread_session()


def get_alerts():
    try:
        yield AlertRepository(get_session_factory())
    finally:
        _release_thread_session()


def get_pending_commands():
    try:
        yield PendingCommandRepository(get_session_factory())
    finally:
        _release_thread_session()


def get_transactions():
    try:
        yield TransactionRepository(get_session_factory())
    finally:
        _release_thread_session()


def get_bot_trades():
    try:
        yield BotTradeRepository(get_session_factory())
    finally:
        _release_thread_session()


# --- Redis dependency ---

_redis = None


def set_redis(redis) -> None:
    """Called once at app startup to wire the Redis connection."""
    global _redis
    _redis = redis


def get_redis():
    """FastAPI dependency: returns the async Redis client, or None."""
    return _redis
