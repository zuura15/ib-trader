"""Regression: a stale ``_expected_disconnect`` flag swallowed real drops.

2026-10-05 and 2026-10-06: the 02:30 PT scheduled reconnect called
``disconnect()`` (flag → True) and nothing ever reset it. When IB Gateway
dropped the API socket at 14:45 PT, ``_on_disconnected`` logged
``"expected": true`` and returned without starting the engine's reconnect
loop, so the engine sat on "Not connected" until a manual restart.

Invariant: one intentional ``disconnect()`` covers exactly one drop, and a
successful ``connect()`` always re-arms unexpected-drop detection.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import ib_trader.engine.ib_resilience as resilience
from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    client = InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )
    fake_ib = MagicMock()
    fake_ib.connectAsync = AsyncMock(return_value=None)
    # Real ib_async fires disconnectedEvent synchronously from disconnect().
    fake_ib.disconnect = MagicMock(side_effect=lambda: client._on_disconnected())
    client._InsyncClient__ib = fake_ib
    client._throttle = AsyncMock(return_value=None)
    return client


async def test_drop_after_scheduled_reconnect_is_unexpected():
    """The exact prod sequence: disconnect → connect → Gateway drop."""
    client = _make_client()
    on_drop = MagicMock()
    client.set_disconnect_callback(on_drop)

    await client.disconnect()   # 02:30 scheduled reconnect, part 1
    on_drop.assert_not_called()
    await client.connect()      # 02:30 scheduled reconnect, part 2

    client._on_disconnected()   # 14:45 Gateway drops the socket
    on_drop.assert_called_once()


async def test_expected_flag_consumed_by_one_drop():
    """Even without a reconnect in between, a second drop is unexpected."""
    client = _make_client()
    on_drop = MagicMock()
    client.set_disconnect_callback(on_drop)

    await client.disconnect()
    client._on_disconnected()
    on_drop.assert_called_once()


async def test_failed_reconnect_hands_off_to_backoff_loop():
    """``reconnect_ib`` drops the socket itself, so a failed connect must
    start the backoff loop explicitly — no disconnect event will."""
    ib = SimpleNamespace(
        disconnect=AsyncMock(return_value=None),
        connect=AsyncMock(side_effect=ConnectionRefusedError("gateway down")),
    )
    ctx = SimpleNamespace(ib=ib, redis=None)

    with patch.object(resilience.asyncio, "sleep", new=AsyncMock()), \
         patch.object(resilience, "_heartbeat_silence_seconds",
                      new=AsyncMock(return_value=None)), \
         patch("ib_trader.engine.main._raise_ib_disconnect_alert") as handoff:
        result = await resilience.reconnect_ib(ctx, source="manual")

    assert result.reconnect_ok is False
    handoff.assert_called_once_with(ctx)
