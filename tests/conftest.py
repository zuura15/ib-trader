"""Shared test fixtures for all test suites.

Provides:
- in_memory_db: In-memory SQLite engine + session factory
- mock_ib: MockIBClient instance
- ctx: Full AppContext with in-memory DB and mock IB
"""
import pytest
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import Base
from ib_trader.data.repository import (
    TradeRepository, RepriceEventRepository,
    ContractRepository, HeartbeatRepository, AlertRepository,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.engine.tracker import OrderTracker
from ib_trader.config.context import AppContext
from ib_trader.ib.base import IBClientBase


class MockIBClient(IBClientBase):
    """Mock IB client for testing. No live connection required.

    Records all calls for assertion in tests.
    Configurable return values per method.
    """

    def __init__(self):
        super().__init__(min_call_interval_ms=0)  # No throttle in tests
        self.connected = False
        self.placed_orders: list[dict] = []
        self.amended_orders: list[dict] = []
        self.canceled_orders: list[str] = []
        self.fill_callbacks: dict[str, list] = {}
        self.status_callbacks: dict[str, list] = {}
        self._next_order_id = 1000
        self._market_snapshot = {"bid": Decimal("100.00"), "ask": Decimal("100.10"), "last": Decimal("100.05")}
        self._order_statuses: dict[str, dict] = {}
        self._qualify_result = {
            "con_id": 12345,
            "exchange": "SMART",
            "currency": "USD",
            "multiplier": None,
            "raw": '{"conId": 12345}',
        }
        # Futures fixtures — tests can extend/replace via attribute.
        # Each root maps to a list of (trading_class, multiplier, tick_size)
        # with a shared set of upcoming expiries. Tests that need a specific
        # layout override ``self.future_fixtures``.
        self.future_fixtures: dict[str, list[dict]] = {
            "ES": [
                {"trading_class": "ES", "multiplier": "50", "tick_size": "0.25", "exchange": "CME", "con_id_base": 400000},
                {"trading_class": "MES", "multiplier": "5", "tick_size": "0.25", "exchange": "CME", "con_id_base": 500000},
            ],
            "MES": [
                {"trading_class": "MES", "multiplier": "5", "tick_size": "0.25", "exchange": "CME", "con_id_base": 500000},
            ],
            "NQ": [
                {"trading_class": "NQ", "multiplier": "20", "tick_size": "0.25", "exchange": "CME", "con_id_base": 600000},
            ],
        }
        # Default upcoming expiries (1 yr out). Tests override when they
        # need a specific month or an expired contract.
        from datetime import date as _date
        today = _date.today()
        self.future_expiries: list[str] = [
            f"{today.year}{m:02d}19" for m in (3, 6, 9, 12)
            if _date(today.year, m, 19) >= today
        ]
        if not self.future_expiries:
            self.future_expiries = [f"{today.year + 1}0319"]

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def managed_accounts(self) -> list[str]:
        """Tests default to a single paper account. Override via attribute
        (``mock_ib.mock_managed_accounts = [...]``) when a test needs to
        exercise mismatch detection."""
        return getattr(self, "mock_managed_accounts", ["DU0000000"])

    async def qualify_contract(
        self,
        symbol,
        sec_type="STK",
        exchange="SMART",
        currency="USD",
        *,
        expiry=None,
        trading_class=None,
    ) -> dict:
        await self._throttle()
        if sec_type.upper() != "FUT":
            return self._qualify_result
        return self._qualify_future_fixture(symbol, exchange, expiry, trading_class)

    def _qualify_future_fixture(self, root, exchange, expiry, trading_class):
        from datetime import date as _date
        from ib_trader.broker.exceptions import AmbiguousInstrument, ExpiredContractError

        if not expiry:
            raise ValueError("FUT qualify requires expiry (YYYYMM or YYYYMMDD)")
        # Normalise expiry shape
        if len(str(expiry)) == 6:
            # YYYYMM → pretend the contract trades on the 19th of that month
            expiry_full = f"{expiry}19"
        else:
            expiry_full = str(expiry)
        # Past-expiry check
        y, m, d = int(expiry_full[:4]), int(expiry_full[4:6]), int(expiry_full[6:8])
        if _date(y, m, d) < _date.today():
            raise ExpiredContractError(root, expiry_full)

        fams = self.future_fixtures.get(root.upper(), [])
        if not fams:
            raise ValueError(f"no IB contract matched {root} {expiry} on {exchange}")
        if trading_class:
            fams = [f for f in fams if f["trading_class"].upper() == trading_class.upper()]
            if not fams:
                raise ValueError(f"no IB contract matched trading_class={trading_class}")
        if len(fams) > 1:
            from ib_trader.broker.types import FutureExpiryCandidate
            candidates = [
                FutureExpiryCandidate(
                    con_id=f["con_id_base"] + int(expiry_full),
                    root=root, expiry=expiry_full,
                    trading_class=f["trading_class"], exchange=f["exchange"],
                    multiplier=Decimal(f["multiplier"]), tick_size=Decimal(f["tick_size"]),
                )
                for f in fams
            ]
            raise AmbiguousInstrument(root=root, candidates=candidates)

        f = fams[0]
        con_id = f["con_id_base"] + int(expiry_full)
        return {
            "con_id": con_id,
            "exchange": f["exchange"],
            "currency": "USD",
            "multiplier": f["multiplier"],
            "trading_class": f["trading_class"],
            "expiry": expiry_full,
            "tick_size": f["tick_size"],
            "raw": f'{{"conId": {con_id}}}',
        }

    async def list_future_expiries(self, root, exchange="CME", trading_class=None, currency="USD"):
        from ib_trader.broker.types import FutureExpiryCandidate

        fams = self.future_fixtures.get(root.upper(), [])
        if trading_class:
            fams = [f for f in fams if f["trading_class"].upper() == trading_class.upper()]
        out: list[FutureExpiryCandidate] = []
        for f in fams:
            if f["exchange"].upper() != exchange.upper():
                continue
            for expiry_full in self.future_expiries:
                out.append(FutureExpiryCandidate(
                    con_id=f["con_id_base"] + int(expiry_full),
                    root=root, expiry=expiry_full,
                    trading_class=f["trading_class"], exchange=f["exchange"],
                    multiplier=Decimal(f["multiplier"]), tick_size=Decimal(f["tick_size"]),
                ))
        out.sort(key=lambda c: c.expiry)
        return out

    async def get_market_snapshot(self, con_id: int) -> dict:
        await self._throttle()
        return self._market_snapshot

    async def place_limit_order(self, con_id, symbol, side, qty, price,
                                outside_rth=True, tif="GTC", order_ref=None) -> str:
        await self._throttle()
        ib_id = str(self._next_order_id)
        self._next_order_id += 1
        self.placed_orders.append({
            "ib_order_id": ib_id, "con_id": con_id, "symbol": symbol,
            "side": side, "qty": qty, "price": price, "tif": tif,
            "order_ref": order_ref,
        })
        self._order_statuses[ib_id] = {
            "status": "Submitted",
            "qty_filled": Decimal("0"),
            "avg_fill_price": None,
            "commission": None,
        }
        return ib_id

    async def place_market_order(self, con_id, symbol, side, qty,
                                 outside_rth=True, order_ref=None) -> str:
        await self._throttle()
        ib_id = str(self._next_order_id)
        self._next_order_id += 1
        self.placed_orders.append({
            "ib_order_id": ib_id, "con_id": con_id, "symbol": symbol,
            "side": side, "qty": qty, "type": "MARKET",
            "order_ref": order_ref,
        })
        self._order_statuses[ib_id] = {
            "status": "Submitted",
            "qty_filled": Decimal("0"),
            "avg_fill_price": None,
            "commission": None,
        }
        return ib_id

    async def amend_order(self, ib_order_id: str, new_price: Decimal) -> None:
        await self._throttle()
        self.amended_orders.append({"ib_order_id": ib_order_id, "new_price": new_price})

    async def cancel_order(self, ib_order_id: str) -> None:
        await self._throttle()
        self.canceled_orders.append(ib_order_id)
        if ib_order_id in self._order_statuses:
            self._order_statuses[ib_order_id]["status"] = "Cancelled"

    async def get_order_status(self, ib_order_id: str) -> dict:
        await self._throttle()
        return self._order_statuses.get(ib_order_id, {
            "status": "UNKNOWN",
            "qty_filled": Decimal("0"),
            "avg_fill_price": None,
            "commission": None,
        })

    async def get_open_orders(self) -> list[dict]:
        await self._throttle()
        return []

    def get_order_error(self, ib_order_id: str) -> str | None:
        return None  # Mock never injects IB errors by default

    async def subscribe_market_data(self, con_id: int, symbol: str) -> None:
        pass  # No-op in mock

    async def unsubscribe_market_data(self, con_id: int) -> None:
        pass  # No-op in mock

    def get_ticker(self, con_id: int) -> dict | None:
        return None  # No streaming data in mock

    def has_contract_cached(self, con_id: int) -> bool:
        return True  # Mock always reports cache hit to skip re-qualification

    def register_fill_callback(self, callback, ib_order_id: str | None = None) -> None:
        key = ib_order_id or "_GLOBAL"
        self.fill_callbacks.setdefault(key, []).append(callback)

    def register_status_callback(self, callback, ib_order_id: str | None = None) -> None:
        key = ib_order_id or "_GLOBAL"
        self.status_callbacks.setdefault(key, []).append(callback)

    def unregister_callbacks(self, ib_order_id: str) -> None:
        """Remove all callbacks registered for an order."""
        self.fill_callbacks.pop(ib_order_id, None)
        self.status_callbacks.pop(ib_order_id, None)

    async def subscribe_realtime_bars(self, con_id, symbol, what_to_show="TRADES", callback=None):
        pass  # No-op in mock

    async def unsubscribe_realtime_bars(self, con_id):
        pass  # No-op in mock

    async def simulate_fill(self, ib_order_id: str, qty: Decimal, price: Decimal,
                            commission: Decimal = Decimal("1.00")) -> None:
        """Simulate a fill event for testing."""
        self._order_statuses[ib_order_id] = {
            "status": "Filled",
            "qty_filled": qty,
            "avg_fill_price": price,
            "commission": commission,
        }
        # Dispatch to order-specific callbacks, then global callbacks.
        for cb in self.fill_callbacks.get(ib_order_id, []):
            await cb(ib_order_id, qty, price, commission)
        for cb in self.fill_callbacks.get("_GLOBAL", []):
            await cb(ib_order_id, qty, price, commission)


@pytest.fixture
def in_memory_engine():
    """Create an in-memory SQLite engine for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(in_memory_engine):
    """Create a scoped session factory for the in-memory DB."""
    factory = sessionmaker(bind=in_memory_engine)
    return scoped_session(factory)


@pytest.fixture
def mock_ib():
    """Create a fresh MockIBClient."""
    return MockIBClient()


@pytest.fixture
def ctx(session_factory, mock_ib):
    """Create a full AppContext with in-memory DB and mock IB."""
    settings = {
        "max_order_size_shares": 10,
        "max_retries": 3,
        "retry_delay_seconds": 2,
        "retry_backoff_multiplier": 2.0,
        "reprice_steps": 10,
        "reprice_active_duration_seconds": 0.1,
        "reprice_passive_wait_seconds": 0.1,
        "market_order_wait_seconds": 1.0,  # Fast SMART_MARKET residual timeout
        "ib_host": "127.0.0.1",
        "ib_port": 7497,
        "ib_client_id": 1,
        "ib_min_call_interval_ms": 0,
        "cache_ttl_seconds": 86400,
        "log_level": "INFO",
        "log_file_path": "logs/test.log",
        "log_rotation_max_bytes": 10485760,
        "log_rotation_backup_count": 10,
        "log_compress_old": False,
        "heartbeat_interval_seconds": 30,
        "heartbeat_stale_threshold_seconds": 300,
        "reconciliation_interval_seconds": 1800,
        "db_integrity_check_interval_seconds": 21600,
        "daemon_tui_refresh_seconds": 5,
    }
    return AppContext(
        ib=mock_ib,
        trades=TradeRepository(session_factory),
        reprice_events=RepriceEventRepository(session_factory),
        contracts=ContractRepository(session_factory),
        heartbeats=HeartbeatRepository(session_factory),
        alerts=AlertRepository(session_factory),
        tracker=OrderTracker(),
        settings=settings,
        account_id="U1234567",
        transactions=TransactionRepository(session_factory),
        pending_commands=PendingCommandRepository(session_factory),
    )
