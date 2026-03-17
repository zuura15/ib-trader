"""IB broker client — adapts the existing InsyncClient to BrokerClientBase.

This is a thin wrapper that translates between the broker-agnostic interface
(string asset_id, Instrument, Snapshot, etc.) and the IB-specific InsyncClient
(int con_id, dict returns, callback registration).

The actual IB API communication still lives in ib_trader.ib.insync_client.
This wrapper just adapts the interface.
"""
import json
import logging
from decimal import Decimal

from ib_trader.broker.base import BrokerClientBase
from ib_trader.broker.types import (
    BrokerCapabilities, Instrument, Snapshot, OrderResult,
)
from ib_trader.broker.fill_stream import FillStream
from ib_trader.broker.market_hours import MarketHoursProvider
from ib_trader.broker.ib.hours import IBMarketHours

logger = logging.getLogger(__name__)


class IBFillStream(FillStream):
    """IB fill stream backed by InsyncClient's callback system.

    Wraps the existing register_fill_callback into the FillStream interface.
    wait_for_fill() uses asyncio.Event to bridge IB's callback dispatch.

    Note: IB dispatches callbacks via asyncio.ensure_future within the event
    loop, so the callback IS awaited. We use async def for the callback.
    """

    def __init__(self, insync_client):
        super().__init__()
        self._client = insync_client
        import asyncio
        self._watches: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        """Register a global fill callback with IB."""
        self._client.register_fill_callback(self._on_fill)

    async def stop(self) -> None:
        pass

    async def _on_fill(self, ib_order_id: str, qty_filled: Decimal,
                        avg_price: Decimal, commission: Decimal) -> None:
        """Fill callback — dispatched by InsyncClient as an asyncio task."""
        from ib_trader.broker.types import FillResult
        self._results[ib_order_id] = FillResult(
            broker_order_id=ib_order_id,
            qty_filled=qty_filled,
            avg_fill_price=avg_price,
            commission=commission,
        )
        event = self._watches.get(ib_order_id)
        if event:
            event.set()

    async def wait_for_fill(self, broker_order_id: str,
                             timeout: float = 30.0):
        import asyncio
        event = asyncio.Event()
        self._watches[broker_order_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._results.get(broker_order_id)
        except asyncio.TimeoutError:
            return None
        finally:
            self._watches.pop(broker_order_id, None)


class IBClient(BrokerClientBase):
    """IB broker client wrapping InsyncClient.

    Translates broker-agnostic interface to IB-specific calls.
    All actual IB communication is delegated to self._insync.
    """

    _CAPABILITIES = BrokerCapabilities(
        supports_in_place_amend=True,
        supports_extended_hours=True,
        supports_overnight=True,
        supports_fractional_shares=False,
        supports_stop_orders=True,
        commission_free=False,
        fill_delivery="push",
        rate_limit_interval_ms=100,
        max_concurrent_connections=32,
    )

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        account_id: str,
        min_call_interval_ms: int = 100,
        market_data_type: int = 3,
    ) -> None:
        super().__init__(min_call_interval_ms=min_call_interval_ms)
        # Lazy import to avoid pulling ib_async into tests
        from ib_trader.ib.insync_client import InsyncClient
        self._insync = InsyncClient(
            host=host,
            port=port,
            client_id=client_id,
            account_id=account_id,
            min_call_interval_ms=min_call_interval_ms,
            market_data_type=market_data_type,
        )
        self._market_hours = IBMarketHours()

    @property
    def broker_name(self) -> str:
        return "ib"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._CAPABILITIES

    @property
    def market_hours(self) -> MarketHoursProvider:
        return self._market_hours

    async def connect(self) -> None:
        await self._insync.connect()

    async def disconnect(self) -> None:
        await self._insync.disconnect()

    async def resolve_instrument(self, symbol: str, **kwargs) -> Instrument:
        result = await self._insync.qualify_contract(
            symbol,
            sec_type=kwargs.get("sec_type", "STK"),
            exchange=kwargs.get("exchange", "SMART"),
            currency=kwargs.get("currency", "USD"),
        )
        return Instrument(
            asset_id=str(result["con_id"]),
            symbol=symbol,
            exchange=result["exchange"],
            currency=result["currency"],
            multiplier=result.get("multiplier"),
            broker="ib",
            raw=result.get("raw", "{}"),
        )

    def has_instrument_cached(self, asset_id: str) -> bool:
        try:
            return self._insync.has_contract_cached(int(asset_id))
        except (ValueError, TypeError):
            return False

    async def get_snapshot(self, asset_id: str) -> Snapshot:
        result = await self._insync.get_market_snapshot(int(asset_id))
        return Snapshot(
            bid=result["bid"],
            ask=result["ask"],
            last=result["last"],
        )

    async def place_limit_order(self, asset_id, symbol, side, qty, price,
                                 extended_hours=True, tif="gtc") -> str:
        # IB uses uppercase TIF
        ib_tif = tif.upper() if tif else "GTC"
        return await self._insync.place_limit_order(
            con_id=int(asset_id),
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            outside_rth=extended_hours,
            tif=ib_tif,
        )

    async def place_market_order(self, asset_id, symbol, side, qty,
                                  extended_hours=True) -> str:
        return await self._insync.place_market_order(
            con_id=int(asset_id),
            symbol=symbol,
            side=side,
            qty=qty,
            outside_rth=extended_hours,
        )

    async def amend_order(self, broker_order_id: str, new_price: Decimal) -> str:
        """IB native in-place amendment. Returns the SAME order ID."""
        await self._insync.amend_order(broker_order_id, new_price)
        return broker_order_id

    async def cancel_order(self, broker_order_id: str) -> None:
        await self._insync.cancel_order(broker_order_id)

    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        result = await self._insync.get_order_status(broker_order_id)
        return OrderResult(
            status=result.get("status", "UNKNOWN"),
            qty_filled=result.get("qty_filled", Decimal("0")),
            avg_fill_price=result.get("avg_fill_price"),
            commission=result.get("commission"),
            why_held=result.get("why_held"),
        )

    async def get_open_orders(self) -> list[dict]:
        return await self._insync.get_open_orders()

    def get_order_error(self, broker_order_id: str) -> str | None:
        return self._insync.get_order_error(broker_order_id)

    def get_live_order_status(self, broker_order_id: str) -> str | None:
        return self._insync.get_live_order_status(broker_order_id)

    def create_fill_stream(self) -> FillStream:
        return IBFillStream(self._insync)

    # Legacy compatibility — delegate to InsyncClient directly
    def register_fill_callback(self, callback, ib_order_id=None):
        self._insync.register_fill_callback(callback, ib_order_id)

    def register_status_callback(self, callback, ib_order_id=None):
        self._insync.register_status_callback(callback, ib_order_id)

    def unregister_callbacks(self, ib_order_id):
        self._insync.unregister_callbacks(ib_order_id)
