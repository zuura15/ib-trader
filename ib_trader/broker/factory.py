"""Broker factory — creates broker clients from configuration.

Reads broker name and settings, returns the appropriate BrokerClientBase
implementation. Used by the engine service startup.
"""
import logging

from ib_trader.broker.base import BrokerClientBase

logger = logging.getLogger(__name__)


class BrokerConfigError(Exception):
    """Raised when broker configuration is invalid."""


def create_broker(broker_name: str, settings: dict, env_vars: dict | None = None) -> BrokerClientBase:
    """Create a broker client from configuration.

    Args:
        broker_name: "ib" or "alpaca".
        settings: Loaded settings dict (from settings.yaml).
        env_vars: Loaded environment variables (from .env).

    Returns:
        Configured BrokerClientBase instance (not yet connected).
    """
    env_vars = env_vars or {}

    if broker_name == "ib":
        from ib_trader.broker.ib.client import IBClient
        account_id = env_vars.get("IB_ACCOUNT_ID", "")
        if not account_id:
            raise BrokerConfigError("IB_ACCOUNT_ID must be set in .env")
        return IBClient(
            host=settings.get("ib_host", "127.0.0.1"),
            port=settings.get("ib_port", 4001),
            client_id=settings.get("ib_client_id", 1),
            account_id=account_id,
            min_call_interval_ms=settings.get("ib_min_call_interval_ms", 100),
            market_data_type=settings.get("ib_market_data_type", 3),
        )

    elif broker_name == "alpaca":
        from ib_trader.broker.alpaca.client import AlpacaClient
        api_key = env_vars.get("ALPACA_API_KEY", "")
        secret_key = env_vars.get("ALPACA_SECRET_KEY", "")
        if not api_key or not secret_key:
            raise BrokerConfigError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env"
            )
        paper = settings.get("alpaca_paper", True)
        return AlpacaClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
        )

    else:
        raise BrokerConfigError(f"Unknown broker: {broker_name}")
