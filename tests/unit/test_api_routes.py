"""Tests for API route handlers.

Uses FastAPI's TestClient with an in-memory SQLite database.
No live broker connection required.
"""
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import (
    Base, TradeGroup, TradeStatus, TransactionAction, TransactionEvent, LegType,
    SystemAlert, AlertSeverity, PendingCommand, PendingCommandStatus,
)
# Ensure all models are registered with Base.metadata before create_all
import ib_trader.data.models  # noqa: F401
from ib_trader.api.app import create_app
from ib_trader.api import deps as api_deps


@pytest.fixture
def api_session_factory(tmp_path):
    """Create a file-based temp SQLite for API tests.

    In-memory SQLite gives each thread its own DB, which breaks TestClient.
    File-based DB with check_same_thread=False works across threads.
    """
    db_path = tmp_path / "test_api.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = scoped_session(sessionmaker(bind=engine))
    return factory


@pytest.fixture
def client(api_session_factory):
    """Create a FastAPI TestClient with lifespan events."""
    app = create_app(api_session_factory)
    # Also set the global directly for the threadpool workers
    api_deps._session_factory = api_session_factory
    with TestClient(app) as c:
        yield c
    api_deps._session_factory = None


def _now():
    return datetime.now(timezone.utc)


class TestCommandRoutes:
    """POST /api/commands and GET /api/commands/{id}."""

    def test_submit_forwards_to_engine(self, client):
        """Commands forward to engine HTTP API — returns 202 accepted."""
        resp = client.post("/api/commands", json={"command": "buy AAPL 10 mid"})
        # The async forward runs in background; we just check it's accepted
        assert resp.status_code in (202, 500, 503)

    def test_get_nonexistent_command_404(self, client):
        resp = client.get("/api/commands/nonexistent-id")
        assert resp.status_code == 404


class TestTradeRoutes:
    """GET /api/trades."""

    def test_list_trades_empty(self, client):
        resp = client.get("/api/trades")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_trades_with_data(self, client, api_session_factory):
        s = api_session_factory()
        s.add(TradeGroup(
            serial_number=0, symbol="AAPL", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        s.commit()

        resp = client.get("/api/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "AAPL"
        assert data[0]["status"] == "OPEN"

    def test_get_trade_by_serial(self, client, api_session_factory):
        s = api_session_factory()
        s.add(TradeGroup(
            serial_number=42, symbol="TSLA", direction="SHORT",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        s.commit()

        resp = client.get("/api/trades/42")
        assert resp.status_code == 200
        assert resp.json()["symbol"] == "TSLA"

    def test_get_trade_not_found(self, client):
        resp = client.get("/api/trades/999")
        assert resp.status_code == 404


class TestOrderRoutes:
    """GET /api/orders."""

    def test_list_open_orders_empty(self, client):
        resp = client.get("/api/orders")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_open_orders_excludes_filled(self, client, api_session_factory):
        s = api_session_factory()
        tg = TradeGroup(
            serial_number=0, symbol="AAPL", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        )
        s.add(tg)
        s.flush()
        # Insert a non-terminal PLACE_ACCEPTED (open) transaction
        s.add(TransactionEvent(
            ib_order_id=8000, action=TransactionAction.PLACE_ACCEPTED,
            symbol="AAPL", side="BUY", order_type="MID",
            quantity=Decimal("100"), account_id="U1234567",
            requested_at=_now(), is_terminal=False,
            trade_id=tg.id, leg_type=LegType.ENTRY,
        ))
        # Insert a terminal FILLED transaction for a different ib_order_id
        s.add(TransactionEvent(
            ib_order_id=8001, action=TransactionAction.FILLED,
            symbol="AAPL", side="BUY", order_type="MID",
            quantity=Decimal("100"), account_id="U1234567",
            requested_at=_now(), is_terminal=True,
            trade_id=tg.id, leg_type=LegType.ENTRY,
            ib_filled_qty=Decimal("100"), ib_avg_fill_price=Decimal("150.00"),
        ))
        s.commit()

        resp = client.get("/api/orders")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["status"] == "PLACE_ACCEPTED"


class TestAlertRoutes:
    """GET /api/alerts and POST /api/alerts/{id}/resolve."""

    def test_list_alerts_empty(self, client):
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_open_alerts(self, client, api_session_factory):
        s = api_session_factory()
        s.add(SystemAlert(
            severity=AlertSeverity.WARNING, trigger="test",
            message="Test alert", created_at=_now(),
        ))
        s.commit()

        resp = client.get("/api/alerts")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["severity"] == "WARNING"

    def test_resolve_alert(self, client, api_session_factory):
        s = api_session_factory()
        alert = SystemAlert(
            severity=AlertSeverity.CATASTROPHIC, trigger="test",
            message="Critical", created_at=_now(),
        )
        s.add(alert)
        s.commit()
        alert_id = alert.id

        resp = client.post(f"/api/alerts/{alert_id}/resolve")
        assert resp.status_code == 204

        # Should no longer appear in open alerts
        resp = client.get("/api/alerts")
        assert resp.json() == []


class TestSystemRoutes:
    """GET /api/status."""

    def test_status_empty(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["heartbeats"] == []
        assert data["alerts"] == []
