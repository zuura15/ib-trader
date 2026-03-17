"""Broker-aware market hours abstraction.

Each broker has different session windows and order constraints per session.
The engine calls these methods instead of using IB-specific market_hours.py
directly. This enables the same engine code to work with both IB's overnight
routing and Alpaca's extended-hours TIF requirements.
"""
from abc import ABC, abstractmethod
from datetime import datetime


class MarketHoursProvider(ABC):
    """Abstract market hours provider. One implementation per broker."""

    @abstractmethod
    def is_session_active(self, now: datetime | None = None) -> bool:
        """True when at least one trading session is open."""
        ...

    @abstractmethod
    def is_extended_hours(self, now: datetime | None = None) -> bool:
        """True during pre-market, after-hours, or overnight sessions."""
        ...

    @abstractmethod
    def session_label(self, now: datetime | None = None) -> str:
        """Human-readable name of the current session window."""
        ...

    @abstractmethod
    def order_params(self, now: datetime | None = None) -> dict:
        """Return session-aware order parameters.

        Returns a dict with keys that the engine passes to place_limit_order:
          - "tif": str — time-in-force for the current session
          - "extended_hours": bool — whether to enable extended-hours routing

        IB example:
          RTH/pre-market/after-hours: {"tif": "GTC", "extended_hours": True}
          Overnight: {"tif": "DAY", "extended_hours": True}

        Alpaca example:
          RTH: {"tif": "gtc", "extended_hours": False}
          Extended hours: {"tif": "day", "extended_hours": True}
        """
        ...

    @abstractmethod
    def supports_market_orders(self, now: datetime | None = None) -> bool:
        """True if market orders are accepted in the current session.

        IB: Always True.
        Alpaca: False during extended hours (limit only).
        """
        ...
