"""Application dependency injection container.

AppContext is created once at process startup and passed to every engine
function, command handler, and background loop.
Nothing constructs its own repositories or IB client.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ib_trader.ib.base import IBClientBase
from ib_trader.data.repository import (
    TradeRepository,
    RepriceEventRepository,
    ContractRepository,
    HeartbeatRepository,
    AlertRepository,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
from ib_trader.data.repositories.template_repository import OrderTemplateRepository
from ib_trader.engine.tracker import OrderTracker
from ib_trader.repl.output_router import OutputRouter


@dataclass
class AppContext:
    """Dependency injection container for the entire application.

    All dependencies are wired at startup and passed through the call stack.
    No global singletons. No module-level imports of live objects.

    ``ib`` is the primary broker connection (IBClientBase). For multi-broker
    support, use ``get_broker(name)`` to access a specific broker. The ``ib``
    field is always the default broker for backward compatibility.

    ``router`` defaults to a buffering OutputRouter so that tests and the
    daemon process do not need to construct one explicitly.  The REPL TUI
    replaces the default router with one connected to the Textual renderer
    via router.set_renderer().
    """

    ib: IBClientBase
    trades: TradeRepository
    reprice_events: RepriceEventRepository
    contracts: ContractRepository
    heartbeats: HeartbeatRepository
    alerts: AlertRepository
    tracker: OrderTracker
    settings: dict          # Loaded from settings.yaml
    account_id: str         # From .env
    transactions: TransactionRepository
    redis: object | None = None  # redis.asyncio.Redis — optional, None until Redis is available
    pending_commands: PendingCommandRepository | None = None
    bots: BotRepository | None = None
    bot_events: BotEventRepository | None = None
    templates: OrderTemplateRepository | None = None
    router: OutputRouter = field(default_factory=OutputRouter)

    # Multi-broker support: dict of broker name → BrokerClientBase
    # When set, get_broker() uses this instead of self.ib
    _brokers: dict | None = field(default=None, repr=False)

    @property
    def broker(self):
        """Primary broker connection (alias for self.ib)."""
        return self.ib

    def get_broker(self, name: str):
        """Get a broker by name. Falls back to self.ib if no multi-broker config.

        Args:
            name: Broker identifier ("ib" or "alpaca").

        Returns:
            BrokerClientBase instance.

        Raises:
            KeyError: If the broker is not configured.
        """
        if self._brokers and name in self._brokers:
            return self._brokers[name]
        # Fallback: if requesting "ib" and no multi-broker, return self.ib
        if name == "ib":
            return self.ib
        raise KeyError(f"Broker '{name}' not configured")
