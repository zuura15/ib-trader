"""Application dependency injection container.

AppContext is created once at process startup and passed to every engine
function, command handler, and background loop.
Nothing constructs its own repositories or IB client.
"""
from dataclasses import dataclass

from ib_trader.ib.base import IBClientBase
from ib_trader.data.repository import (
    TradeRepository,
    OrderRepository,
    RepriceEventRepository,
    ContractRepository,
    HeartbeatRepository,
    AlertRepository,
)
from ib_trader.engine.tracker import OrderTracker


@dataclass
class AppContext:
    """Dependency injection container for the entire application.

    All dependencies are wired at startup and passed through the call stack.
    No global singletons. No module-level imports of live objects.
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
