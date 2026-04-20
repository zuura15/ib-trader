"""REPL heartbeat watcher and alert trigger for the daemon.

Monitors REPL heartbeat every 30 seconds (configurable).
If REPL heartbeat goes stale beyond threshold: triggers CATASTROPHIC alert.
"""
import logging
from datetime import datetime, timezone

from ib_trader.config.context import AppContext
from ib_trader.data.models import AlertSeverity, SystemAlert

logger = logging.getLogger(__name__)


async def check_repl_heartbeat(ctx: AppContext) -> bool:
    """Check REPL heartbeat and raise CATASTROPHIC alert if stale.

    A missing heartbeat row means REPL exited cleanly or was never started (WARNING).
    A stale (present but old) heartbeat row means REPL likely crashed (CATASTROPHIC).

    Args:
        ctx: Application dependency injection container.

    Returns:
        True if REPL is alive, False if stale or missing.
    """
    threshold = ctx.settings["heartbeat_stale_threshold_seconds"]
    hb = ctx.heartbeats.get("REPL")

    if hb is None:
        # Clean exit or not started — not a crash
        logger.debug('{"event": "HEARTBEAT_STALE", "process": "REPL", "reason": "missing"}')
        return False

    last_seen = hb.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last_seen).total_seconds()

    if age > threshold:
        logger.error(
            '{"event": "HEARTBEAT_STALE", "process": "REPL", "age_seconds": %.1f, '
            '"threshold": %d}',
            age, threshold,
        )
        # Raise CATASTROPHIC alert
        existing_open = ctx.alerts.get_open()
        already_alerted = any(a.trigger == "REPL_HEARTBEAT_STALE" for a in existing_open)
        if not already_alerted:
            alert = SystemAlert(
                severity=AlertSeverity.CATASTROPHIC,
                trigger="REPL_HEARTBEAT_STALE",
                message=(
                    f"CLI heartbeat lost (last seen {int(age)}s ago, PID {hb.pid}). "
                    "REPL may have crashed. Check the process and restart if needed."
                ),
                created_at=datetime.now(timezone.utc),
            )
            ctx.alerts.create(alert)
            logger.error('{"event": "SYSTEM_ALERT_RAISED", "severity": "CATASTROPHIC", "trigger": "REPL_HEARTBEAT_STALE"}')
        return False

    return True


async def check_ib_connectivity(ctx: AppContext, consecutive_failures: list) -> bool:
    """Perform a passive IB connectivity check.

    3 consecutive failures triggers a CATASTROPHIC alert.

    Args:
        ctx: Application context.
        consecutive_failures: Mutable list used to track failure count across calls.

    Returns:
        True if IB is reachable, False otherwise.
    """
    try:
        # Lightweight check — get open orders (empty list is fine)
        await ctx.ib.get_open_orders()
        consecutive_failures.clear()
        return True
    except Exception as e:
        consecutive_failures.append(str(e))
        count = len(consecutive_failures)
        logger.warning(
            '{"event": "IB_CONNECTIVITY_CHECK_FAILED", "attempt": %d, "error": "%s"}',
            count, str(e),
        )

        if count >= 3:
            existing_open = ctx.alerts.get_open()
            already_alerted = any(a.trigger == "IB_CONNECTIVITY_FAILURE" for a in existing_open)
            if not already_alerted:
                alert = SystemAlert(
                    severity=AlertSeverity.CATASTROPHIC,
                    trigger="IB_CONNECTIVITY_FAILURE",
                    message=f"IB Gateway unreachable — {count} consecutive failures. Last error: {e!s}",
                    created_at=datetime.now(timezone.utc),
                )
                ctx.alerts.create(alert)
                logger.error(
                    '{"event": "SYSTEM_ALERT_RAISED", "severity": "CATASTROPHIC", '
                    '"trigger": "IB_CONNECTIVITY_FAILURE"}'
                )
        elif count == 1:
            alert = SystemAlert(
                severity=AlertSeverity.WARNING,
                trigger="IB_CONNECTIVITY_WARNING",
                message=f"IB Gateway connectivity check failed (attempt {count}): {e!s}",
                created_at=datetime.now(timezone.utc),
            )
            ctx.alerts.create(alert)
            logger.warning('{"event": "SYSTEM_ALERT_RAISED", "severity": "WARNING", "trigger": "IB_CONNECTIVITY_WARNING"}')

        return False
