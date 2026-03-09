"""SQLite integrity checks for the daemon.

Runs PRAGMA integrity_check on startup and every 6 hours (configurable).
Failure triggers a CATASTROPHIC alert.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import scoped_session

from ib_trader.config.context import AppContext
from ib_trader.data.models import AlertSeverity, SystemAlert
logger = logging.getLogger(__name__)


def run_integrity_check(session_factory: scoped_session, ctx: AppContext) -> bool:
    """Run PRAGMA integrity_check on the SQLite database.

    Args:
        session_factory: SQLAlchemy scoped session factory.
        ctx: Application context.

    Returns:
        True if integrity check passed, False otherwise.

    Side effects:
        - Logs DB_INTEGRITY_PASSED or DB_INTEGRITY_FAILED.
        - Creates a CATASTROPHIC alert if check fails.
    """
    try:
        session = session_factory()
        result = session.execute(text("PRAGMA integrity_check")).fetchall()
        messages = [row[0] for row in result]

        if messages == ["ok"]:
            logger.info('{"event": "DB_INTEGRITY_PASSED"}')
            return True
        else:
            error_details = "; ".join(messages)
            logger.error(
                '{"event": "DB_INTEGRITY_FAILED", "details": "%s"}', error_details
            )
            # Raise CATASTROPHIC alert
            existing_open = ctx.alerts.get_open()
            already_alerted = any(a.trigger == "DB_INTEGRITY_FAILED" for a in existing_open)
            if not already_alerted:
                alert = SystemAlert(
                    severity=AlertSeverity.CATASTROPHIC,
                    trigger="DB_INTEGRITY_FAILED",
                    message=f"SQLite integrity check failed: {error_details}",
                    created_at=datetime.now(timezone.utc),
                )
                ctx.alerts.create(alert)
                logger.error(
                    '{"event": "SYSTEM_ALERT_RAISED", "severity": "CATASTROPHIC", '
                    '"trigger": "DB_INTEGRITY_FAILED"}'
                )
            return False

    except Exception as e:
        logger.error(
            '{"event": "DB_INTEGRITY_FAILED", "error": "%s"}', str(e), exc_info=True
        )
        return False
