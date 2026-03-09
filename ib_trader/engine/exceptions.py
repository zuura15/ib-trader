"""Custom exception hierarchy for IB Trader.

All custom exceptions live here. All modules import from this file.
No exception names are invented elsewhere.
"""


class IBTraderError(Exception):
    """Base exception for all application errors."""


class SafetyLimitError(IBTraderError):
    """Order exceeds configured safety limits."""


class SymbolNotAllowedError(IBTraderError):
    """Symbol not in whitelist."""


class IBConnectionError(IBTraderError):
    """Cannot connect to or communicate with IB."""


class IBOrderRejectedError(IBTraderError):
    """IB rejected the order. Contains rejection reason."""

    def __init__(self, reason: str) -> None:
        """Initialize with rejection reason from IB."""
        self.reason = reason
        super().__init__(f"Order rejected by IB: {reason}")


class ContractNotFoundError(IBTraderError):
    """Could not qualify IB contract for symbol."""


class TradeNotFoundError(IBTraderError):
    """No trade found for given serial number."""


class ConfigurationError(IBTraderError):
    """Invalid or missing configuration."""


class DBIntegrityError(IBTraderError):
    """SQLite integrity check failed."""
