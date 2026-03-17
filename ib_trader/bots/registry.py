"""Bot strategy registry.

Maps strategy names to bot classes. Used by the bot runner to
instantiate the correct bot class from the bots table config.
"""
import logging
from ib_trader.bots.base import BotBase

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type[BotBase]] = {}


def register_strategy(name: str, cls: type[BotBase]) -> None:
    """Register a bot class for a strategy name."""
    _REGISTRY[name] = cls
    logger.info('{"event": "STRATEGY_REGISTERED", "name": "%s", "class": "%s"}',
                name, cls.__name__)


def get_strategy_class(name: str) -> type[BotBase] | None:
    """Return the bot class for a strategy name, or None."""
    return _REGISTRY.get(name)


def list_strategies() -> list[str]:
    """Return all registered strategy names."""
    return list(_REGISTRY.keys())
