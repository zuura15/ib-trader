"""SQLAlchemy ORM models for the IB Trader database.

All monetary values use Numeric(18, 8) mapped to Decimal.
All primary keys are UUID strings generated with uuid4().
All datetimes are stored in server-local timezone.
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
    AMENDED = "AMENDED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCEL_ATTEMPT = "CANCEL_ATTEMPT"
    CANCELLED = "CANCELLED"
    ERROR_TERMINAL = "ERROR_TERMINAL"
    RECONCILED = "RECONCILED"
    DISCREPANCY = "DISCREPANCY"


class PendingCommandStatus(enum.Enum):
    """Lifecycle status of a command in the pending_commands queue."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class BotStatus(enum.Enum):
    """Lifecycle status of a bot."""
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    PAUSED = "PAUSED"


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
    trade_config     = Column(Text, nullable=True)  # JSON: PT amount, PT price, SL


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
    order_id            = Column(String(36), nullable=True)  # Legacy FK, kept for historical rows
    correlation_id      = Column(String(36), nullable=True)  # New: links to transaction correlation_id
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
    around an order. One row per event. Never updated after insert.

    This is the sole record of order state — the orders table has been
    removed. IB is the source of truth for live state; transactions are
    the source of truth for historical state and trade-group linkage.
    """

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

    # --- Columns added to replace the orders table ---
    trade_id          = Column(String(36), ForeignKey("trade_groups.id"), nullable=True)
    leg_type          = Column(Enum(LegType), nullable=True)
    commission        = Column(Numeric(18, 8), nullable=True)
    price_placed      = Column(Numeric(18, 4), nullable=True)
    correlation_id    = Column(String(36), nullable=True)
    security_type     = Column(String(10), nullable=True)
    expiry            = Column(String(10), nullable=True)
    strike            = Column(Numeric(18, 4), nullable=True)
    right             = Column(String(4), nullable=True)
    raw_response      = Column(Text, nullable=True)


class PendingCommand(Base):
    """Command queue for engine-client communication.

    Clients (REPL, API server, bots) insert rows with status=PENDING.
    The engine service polls for PENDING rows, executes them, and updates
    status to SUCCESS or FAILURE with output/error.
    """

    __tablename__ = "pending_commands"

    id           = Column(String(36), primary_key=True, default=_uuid)
    source       = Column(String(50), nullable=False)     # "repl", "api", "bot:<bot_id>"
    broker       = Column(String(20), nullable=False, default="ib")
    command_text = Column(Text, nullable=False)
    status       = Column(Enum(PendingCommandStatus), nullable=False,
                          default=PendingCommandStatus.PENDING)
    output       = Column(Text, nullable=True)
    error        = Column(Text, nullable=True)
    submitted_at = Column(DateTime, nullable=False)
    started_at   = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class Bot(Base):
    """Bot configuration and runtime status.

    State is persisted in SQLite so the bot runner can crash and restart
    with full context (zero memory state principle).
    """

    __tablename__ = "bots"

    id                   = Column(String(36), primary_key=True, default=_uuid)
    name                 = Column(String(100), unique=True, nullable=False)
    strategy             = Column(String(100), nullable=False)
    broker               = Column(String(20), nullable=False, default="ib")
    config_json          = Column(Text, nullable=False, default="{}")
    status               = Column(Enum(BotStatus), nullable=False, default=BotStatus.STOPPED)
    tick_interval_seconds = Column(Integer, nullable=False, default=10)
    last_heartbeat       = Column(DateTime, nullable=True)
    last_signal          = Column(String(500), nullable=True)
    last_action          = Column(String(500), nullable=True)
    last_action_at       = Column(DateTime, nullable=True)
    error_message        = Column(Text, nullable=True)
    trades_total         = Column(Integer, nullable=False, default=0)
    trades_today         = Column(Integer, nullable=False, default=0)
    pnl_today            = Column(Numeric(18, 8), nullable=False, default=0)
    symbols_json         = Column(Text, nullable=False, default="[]")
    created_at           = Column(DateTime, nullable=False)
    updated_at           = Column(DateTime, nullable=False)


class BotEvent(Base):
    """Append-only audit log for bot activity. Never updated after insert.

    Denormalized: carries bot_name / strategy / config_version so old
    events stay readable after the source YAML is edited or deleted.
    The FK to `bots.id` has been dropped — bot identity is now
    authoritative in `config/bots/*.yaml`, and a missing bots row is no
    longer an integrity violation.
    """

    __tablename__ = "bot_events"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    bot_id         = Column(String(36), nullable=False, index=True)
    bot_name       = Column(String(100), nullable=True, index=True)
    strategy       = Column(String(100), nullable=True)
    config_version = Column(String(32), nullable=True)
    event_type     = Column(String(50), nullable=False)    # STARTED, STOPPED, SIGNAL, ACTION, ERROR, HEARTBEAT
    message        = Column(Text, nullable=True)
    payload_json   = Column(Text, nullable=True)           # Structured JSON for machine-readable data
    trade_serial   = Column(Integer, nullable=True)
    recorded_at    = Column(DateTime, nullable=False)


class OrderTemplate(Base):
    """Saved order template for quick-fire orders from the GUI."""

    __tablename__ = "order_templates"

    id         = Column(String(36), primary_key=True, default=_uuid)
    label      = Column(String(200), nullable=False)
    symbol     = Column(String(20), nullable=False)
    side       = Column(String(4), nullable=False)       # BUY / SELL
    quantity   = Column(Numeric(18, 4), nullable=False)
    order_type = Column(String(10), nullable=False)      # LMT / MKT / STP / MOC
    price      = Column(Numeric(18, 4), nullable=True)
    broker     = Column(String(20), nullable=False, default="ib")
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


