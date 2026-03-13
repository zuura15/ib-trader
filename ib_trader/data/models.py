"""SQLAlchemy ORM models for the IB Trader database.

All monetary values use Numeric(18, 8) mapped to Decimal.
All primary keys are UUID strings generated with uuid4().
All datetimes are stored in UTC.
"""
import uuid
from sqlalchemy import (
    Column, String, Integer, Numeric, Boolean,
    DateTime, Enum, ForeignKey, Text
)
from sqlalchemy.orm import declarative_base
import enum


Base = declarative_base()


def _uuid() -> str:
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())


class LegType(enum.Enum):
    """Type of order leg within a trade group."""
    ENTRY = "ENTRY"
    PROFIT_TAKER = "PROFIT_TAKER"
    STOP_LOSS = "STOP_LOSS"
    CLOSE = "CLOSE"


class OrderStatus(enum.Enum):
    """Lifecycle status of an order leg."""
    PENDING = "PENDING"
    OPEN = "OPEN"
    REPRICING = "REPRICING"
    AMENDING = "AMENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELED = "CANCELED"
    ABANDONED = "ABANDONED"
    CLOSED_MANUAL = "CLOSED_MANUAL"
    CLOSED_EXTERNAL = "CLOSED_EXTERNAL"
    REJECTED = "REJECTED"


class SecurityType(enum.Enum):
    """IB security type. OPT and FUT are future — no trading logic in v1."""
    STK = "STK"
    ETF = "ETF"
    OPT = "OPT"   # FUTURE — no trading logic
    FUT = "FUT"   # FUTURE — no trading logic


class TradeStatus(enum.Enum):
    """Overall status of a trade group."""
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PARTIAL = "PARTIAL"


class TransactionAction(str, enum.Enum):
    """Action type for each row in the append-only transactions audit log."""
    PLACE_ATTEMPT = "PLACE_ATTEMPT"
    PLACE_ACCEPTED = "PLACE_ACCEPTED"
    PLACE_REJECTED = "PLACE_REJECTED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCEL_ATTEMPT = "CANCEL_ATTEMPT"
    CANCELLED = "CANCELLED"
    ERROR_TERMINAL = "ERROR_TERMINAL"
    RECONCILED = "RECONCILED"


class AlertSeverity(enum.Enum):
    """Alert severity levels.

    Designed with ordinal spacing so additional levels can be inserted
    between CATASTROPHIC and WARNING without restructuring.
    """
    CATASTROPHIC = "CATASTROPHIC"
    WARNING = "WARNING"


class TradeGroup(Base):
    """A trade group linking all legs of a single trade (entry, profit taker, close, etc.)."""

    __tablename__ = "trade_groups"

    id               = Column(String(36), primary_key=True, default=_uuid)
    serial_number    = Column(Integer, unique=True, nullable=False)
    symbol           = Column(String(20), nullable=False)
    direction        = Column(String(5), nullable=False)    # LONG / SHORT
    status           = Column(Enum(TradeStatus), nullable=False, default=TradeStatus.OPEN)
    realized_pnl     = Column(Numeric(18, 8), nullable=True)
    total_commission = Column(Numeric(18, 8), nullable=True)
    opened_at        = Column(DateTime, nullable=False)
    closed_at        = Column(DateTime, nullable=True)


class Order(Base):
    """A single order leg within a trade group."""

    __tablename__ = "orders"

    id                  = Column(String(36), primary_key=True, default=_uuid)
    trade_id            = Column(String(36), ForeignKey("trade_groups.id"), nullable=False)
    serial_number       = Column(Integer, nullable=True)        # entry leg only
    ib_order_id         = Column(String(50), nullable=True)
    leg_type            = Column(Enum(LegType), nullable=False)
    symbol              = Column(String(20), nullable=False)
    side                = Column(String(4), nullable=False)     # BUY / SELL
    security_type       = Column(Enum(SecurityType), nullable=False, default=SecurityType.STK)
    expiry              = Column(String(10), nullable=True)     # YYYYMMDD
    strike              = Column(Numeric(18, 4), nullable=True)
    right               = Column(String(4), nullable=True)      # CALL / PUT
    qty_requested       = Column(Numeric(18, 4), nullable=False)
    qty_filled          = Column(Numeric(18, 4), nullable=False, default=0)
    order_type          = Column(String(10), nullable=False)    # MID / MARKET
    price_placed        = Column(Numeric(18, 4), nullable=True)
    avg_fill_price      = Column(Numeric(18, 4), nullable=True)
    profit_taker_amount = Column(Numeric(18, 4), nullable=True)
    profit_taker_price  = Column(Numeric(18, 4), nullable=True)
    stop_loss_requested = Column(Numeric(18, 4), nullable=True)  # stored, no IB action
    commission          = Column(Numeric(18, 8), nullable=True)
    status              = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.PENDING)
    placed_at           = Column(DateTime, nullable=True)
    filled_at           = Column(DateTime, nullable=True)
    canceled_at         = Column(DateTime, nullable=True)
    last_amended_at     = Column(DateTime, nullable=True)
    raw_ib_response     = Column(Text, nullable=True)           # JSON string


class RepriceEvent(Base):
    """A single reprice step for an order during the reprice loop."""

    __tablename__ = "reprice_events"

    id                  = Column(String(36), primary_key=True, default=_uuid)
    order_id            = Column(String(36), ForeignKey("orders.id"), nullable=False)
    step_number         = Column(Integer, nullable=False)
    bid                 = Column(Numeric(18, 4), nullable=False)
    ask                 = Column(Numeric(18, 4), nullable=False)
    new_price           = Column(Numeric(18, 4), nullable=False)
    amendment_confirmed = Column(Boolean, nullable=False, default=False)
    timestamp           = Column(DateTime, nullable=False)


class Contract(Base):
    """Cached IB contract details to avoid repeated qualification requests."""

    __tablename__ = "contracts"

    symbol       = Column(String(20), primary_key=True)
    con_id       = Column(Integer, nullable=False)
    exchange     = Column(String(20), nullable=False)
    currency     = Column(String(5), nullable=False)
    multiplier   = Column(String(10), nullable=True)
    raw_response = Column(Text, nullable=True)
    fetched_at   = Column(DateTime, nullable=False)


class Metric(Base):
    """Time-series metric event for analytics and reporting."""

    __tablename__ = "metrics"

    id          = Column(String(36), primary_key=True, default=_uuid)
    trade_id    = Column(String(36), ForeignKey("trade_groups.id"), nullable=True)
    event_type  = Column(String(50), nullable=False)
    symbol      = Column(String(20), nullable=True)
    value       = Column(Numeric(18, 8), nullable=True)
    meta        = Column(Text, nullable=True)    # JSON string
    recorded_at = Column(DateTime, nullable=False)


class SystemHeartbeat(Base):
    """Process liveness heartbeat for mutual watchdog between REPL and daemon."""

    __tablename__ = "system_heartbeats"

    process      = Column(String(10), primary_key=True)  # REPL / DAEMON
    last_seen_at = Column(DateTime, nullable=False)
    pid          = Column(Integer, nullable=True)


class SystemAlert(Base):
    """Alert raised by the daemon for CATASTROPHIC or WARNING conditions."""

    __tablename__ = "system_alerts"

    id          = Column(String(36), primary_key=True, default=_uuid)
    severity    = Column(Enum(AlertSeverity), nullable=False)
    trigger     = Column(String(100), nullable=False)
    message     = Column(Text, nullable=False)
    created_at  = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)


class TransactionEvent(Base):
    """Append-only audit log of every interaction our system has with IB
    around an order. One row per event. Never updated after insert."""

    __tablename__ = "transactions"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    ib_order_id       = Column(Integer, nullable=True)
    ib_perm_id        = Column(Integer, nullable=True)
    action            = Column(Enum(TransactionAction), nullable=False)
    symbol            = Column(String(20), nullable=False)
    side              = Column(String(4), nullable=False)
    order_type        = Column(String(10), nullable=False)
    quantity          = Column(Numeric(18, 4), nullable=False)
    limit_price       = Column(Numeric(18, 4), nullable=True)
    account_id        = Column(String(50), nullable=False)
    ib_status         = Column(String(50), nullable=True)
    ib_filled_qty     = Column(Numeric(18, 4), nullable=True)
    ib_avg_fill_price = Column(Numeric(18, 8), nullable=True)
    ib_error_code     = Column(Integer, nullable=True)
    ib_error_message  = Column(Text, nullable=True)
    trade_serial      = Column(Integer, nullable=True)
    requested_at      = Column(DateTime, nullable=False)
    ib_responded_at   = Column(DateTime, nullable=True)
    is_terminal       = Column(Boolean, nullable=False, default=False)
