"""Application dependency injection container.

AppContext is created once at process startup and passed to every engine
function, command handler, and background loop.
Nothing constructs its own repositories or IB client.
"""
from dataclasses import dataclass, field

from ib_trader.ib.base import IBClientBase
from ib_trader.data.repository import (
    TradeRepository,
    OrderRepository,
    RepriceEventRepository,
    ContractRepository,
    HeartbeatRepository,
    AlertRepository,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.engine.tracker import OrderTracker
from ib_trader.repl.output_router import OutputRouter


@dataclass
class AppContext:
    """Dependency injection container for the entire application.

    All dependencies are wired at startup and passed through the call stack.
    No global singletons. No module-level imports of live objects.

    ``router`` defaults to a buffering OutputRouter so that tests and the
    daemon process do not need to construct one explicitly.  The REPL TUI
    replaces the default router with one connected to the Textual renderer
    via router.set_renderer().
    """

    ib: IBClientBase
    trades: TradeRepository
    orders: OrderRepository
    reprice_events: RepriceEventRepository
    contracts: ContractRepository
    heartbeats: HeartbeatRepository
    alerts: AlertRepository
    tracker: OrderTracker
    settings: dict          # Loaded from settings.yaml
    account_id: str         # From .env
    transactions: TransactionRepository | None = None
    router: OutputRouter = field(default_factory=OutputRouter)
