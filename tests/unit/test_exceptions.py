"""Unit tests for custom exception hierarchy."""
import pytest

from ib_trader.engine.exceptions import (
    IBTraderError, SafetyLimitError, SymbolNotAllowedError,
    IBConnectionError, IBOrderRejectedError, ContractNotFoundError,
    TradeNotFoundError, ConfigurationError, DBIntegrityError,
)


class TestExceptionHierarchy:
    def test_all_exceptions_are_ibtrader_errors(self):
        assert issubclass(SafetyLimitError, IBTraderError)
        assert issubclass(SymbolNotAllowedError, IBTraderError)
        assert issubclass(IBConnectionError, IBTraderError)
        assert issubclass(IBOrderRejectedError, IBTraderError)
        assert issubclass(ContractNotFoundError, IBTraderError)
        assert issubclass(TradeNotFoundError, IBTraderError)
        assert issubclass(ConfigurationError, IBTraderError)
        assert issubclass(DBIntegrityError, IBTraderError)

    def test_ib_order_rejected_stores_reason(self):
        exc = IBOrderRejectedError("Insufficient funds")
        assert exc.reason == "Insufficient funds"
        assert "Insufficient funds" in str(exc)

    def test_ibtrader_error_is_exception(self):
        assert issubclass(IBTraderError, Exception)

    def test_raise_and_catch_base(self):
        with pytest.raises(IBTraderError):
            raise SafetyLimitError("too large")

    def test_raise_and_catch_specific(self):
        with pytest.raises(SafetyLimitError):
            raise SafetyLimitError("too large")
