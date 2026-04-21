"""Pydantic models for API request/response serialization.

All Decimal fields serialize as strings to avoid float precision loss.
All datetimes serialize as ISO 8601 UTC strings.
"""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class CommandRequest(BaseModel):
    """Request body for POST /api/commands."""
    command: str
    broker: str = "ib"
    command_id: str | None = None  # Client-supplied id; keys the live-output Redis stream so the frontend can subscribe before the POST returns.


class CommandResponse(BaseModel):
    """Response for command submission."""
    command_id: str
    status: str
    output: str | None = None  # Set when synchronous (read-only commands or completed orders)


class CommandStatusResponse(BaseModel):
    """Response for GET /api/commands/{id}."""
    command_id: str
    status: str
    command_text: str
    source: str
    output: str | None = None
    error: str | None = None
    submitted_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TradeResponse(BaseModel):
    """Serialized trade group."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    serial_number: int
    symbol: str
    direction: str
    status: str
    realized_pnl: str | None = None     # Decimal as string
    total_commission: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    # Augmented fill detail from the transaction legs — populated by
    # /api/trades so the Trades panel can render a data-rich row
    # (qty, entry/exit price, P&L%) without a second API call.
    entry_qty: str | None = None         # ib_filled_qty on the entry fill
    entry_price: str | None = None       # ib_avg_fill_price on the entry fill
    exit_qty: str | None = None          # ib_filled_qty summed across CLOSE legs
    exit_price: str | None = None        # weighted avg across CLOSE fills
    order_type: str | None = None        # order_type on the entry leg (mid/market/...)


class OrderResponse(BaseModel):
    """Serialized order leg."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    trade_id: str
    serial_number: int | None = None
    ib_order_id: str | None = None
    leg_type: str
    symbol: str
    side: str
    security_type: str
    qty_requested: str          # Decimal as string
    qty_filled: str
    order_type: str
    price_placed: str | None = None
    avg_fill_price: str | None = None
    commission: str | None = None
    status: str
    placed_at: datetime | None = None
    filled_at: datetime | None = None


class AlertResponse(BaseModel):
    """Serialized system alert."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    severity: str
    trigger: str
    message: str
    created_at: datetime
    resolved_at: datetime | None = None


class HeartbeatResponse(BaseModel):
    """Serialized heartbeat."""
    process: str
    last_seen_at: datetime
    pid: int | None = None


class SystemStatusResponse(BaseModel):
    """Response for GET /api/status."""
    heartbeats: list[HeartbeatResponse]
    alerts: list[AlertResponse]


class TemplateRequest(BaseModel):
    """Request body for POST /api/templates."""
    label: str
    symbol: str
    side: str
    quantity: str           # Decimal as string
    order_type: str
    price: str | None = None
    broker: str = "ib"


class TemplateResponse(BaseModel):
    """Serialized order template."""
    id: str
    label: str
    symbol: str
    side: str
    quantity: str
    order_type: str
    price: str | None = None
    broker: str
