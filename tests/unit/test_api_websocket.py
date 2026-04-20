"""Tests for the WebSocket endpoint.

Covers: connection, subscribe, snapshot delivery, ping/pong, diff detection.
"""
import json
import pytest
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import (
    Base, TradeGroup, TradeStatus,
)
from ib_trader.api.app import create_app
from ib_trader.api import deps as api_deps


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def ws_session_factory(tmp_path):
    db_path = tmp_path / "test_ws.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = scoped_session(sessionmaker(bind=engine))
    return factory


@pytest.fixture
def ws_client(ws_session_factory):
    app = create_app(ws_session_factory)
    api_deps._session_factory = ws_session_factory
    with TestClient(app) as c:
        yield c
    api_deps._session_factory = None


class TestWebSocketConnection:
    """Basic WebSocket connection and protocol tests."""

    def test_connect_and_subscribe(self, ws_client):
        with ws_client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({
                "type": "subscribe",
                "channels": ["trades", "orders", "alerts"],
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "snapshot"
            assert "trades" in resp["data"]
            assert "orders" in resp["data"]
            assert "alerts" in resp["data"]
            assert isinstance(resp["data"]["trades"], list)

    def test_ping_pong(self, ws_client):
        with ws_client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "ping"}))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "pong"

    def test_subscribe_invalid_channel_ignored(self, ws_client):
        with ws_client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({
                "type": "subscribe",
                "channels": ["invalid_channel", "trades"],
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "snapshot"
            # Only valid channels in snapshot
            assert "trades" in resp["data"]
            assert "invalid_channel" not in resp["data"]

    def test_empty_snapshot(self, ws_client):
        with ws_client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({
                "type": "subscribe",
                "channels": ["trades"],
            }))
            resp = json.loads(ws.receive_text())
            assert resp["data"]["trades"] == []


class TestWebSocketData:
    """Snapshot contains actual data from SQLite."""

    def test_snapshot_includes_trades(self, ws_client, ws_session_factory):
        s = ws_session_factory()
        s.add(TradeGroup(
            serial_number=0, symbol="AAPL", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        s.commit()

        with ws_client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({
                "type": "subscribe",
                "channels": ["trades"],
            }))
            resp = json.loads(ws.receive_text())
            trades = resp["data"]["trades"]
            assert len(trades) == 1
            assert trades[0]["symbol"] == "AAPL"
            assert trades[0]["status"] == "OPEN"

    def test_snapshot_alerts_empty_without_redis(self, ws_client):
        """Without Redis, alerts snapshot returns [] (alerts now in Redis)."""
        with ws_client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({
                "type": "subscribe",
                "channels": ["alerts"],
            }))
            resp = json.loads(ws.receive_text())
            alerts = resp["data"]["alerts"]
            assert alerts == []


class TestDiffComputation:
    """Test the diff computation helpers."""

    def test_compute_diff_added(self):
        from ib_trader.api.ws import _compute_diff
        old = [{"id": "1", "name": "a"}]
        new = [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]
        diff = _compute_diff(old, new)
        assert len(diff["added"]) == 1
        assert diff["added"][0]["id"] == "2"
        assert diff["updated"] == []
        assert diff["removed"] == []

    def test_compute_diff_removed(self):
        from ib_trader.api.ws import _compute_diff
        old = [{"id": "1"}, {"id": "2"}]
        new = [{"id": "1"}]
        diff = _compute_diff(old, new)
        assert len(diff["removed"]) == 1
        assert diff["removed"][0]["id"] == "2"

    def test_compute_diff_updated(self):
        from ib_trader.api.ws import _compute_diff
        old = [{"id": "1", "status": "OPEN"}]
        new = [{"id": "1", "status": "FILLED"}]
        diff = _compute_diff(old, new)
        assert len(diff["updated"]) == 1
        assert diff["updated"][0]["status"] == "FILLED"

    def test_compute_diff_no_change(self):
        from ib_trader.api.ws import _compute_diff
        data = [{"id": "1", "name": "a"}]
        diff = _compute_diff(data, data)
        assert diff["added"] == []
        assert diff["updated"] == []
        assert diff["removed"] == []

    def test_channel_state_detects_change(self):
        from ib_trader.api.ws import _ChannelState
        state = _ChannelState("trades")
        # First update always returns diff (from empty)
        diff = state.update([{"id": "1", "v": "a"}])
        assert diff is not None
        assert len(diff["added"]) == 1

        # Same data — no diff
        diff = state.update([{"id": "1", "v": "a"}])
        assert diff is None

        # Changed data — returns diff
        diff = state.update([{"id": "1", "v": "b"}])
        assert diff is not None
        assert len(diff["updated"]) == 1
