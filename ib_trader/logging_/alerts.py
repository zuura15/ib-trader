"""Shared helper for the "catch → log at ERROR → surface to UI" contract.

Project invariant (see CLAUDE.md): no silent ``except`` blocks in the
engine, middleware, bot runtime, or bot strategies. Every caught
exception must either re-raise OR funnel through ``log_and_alert`` so
the failure is both logged at ERROR level AND visible in the UI alerts
panel.

This helper is intentionally small. Callers should supply a trigger
name (a stable string tag for categorising the failure) and a message.
Severity defaults to WARNING for application-level issues; broker-
facing code paths (anything wrapping a ``ctx.ib.*`` call) should pass
``severity="CATASTROPHIC"`` so the frontend renders a blocking modal.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fire_and_forget_alert(
    *,
    redis,
    trigger: str,
    message: str,
    severity: str = "WARNING",
    bot_id: str | None = None,
    symbol: str | None = None,
    ib_order_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Schedule ``log_and_alert`` without awaiting. Safe to call from
    sync code. If no event loop is running (test harnesses, bootstrap
    code), falls back to a plain ``logger.warning`` so the signal is
    not lost and no coroutine is leaked.
    """
    import asyncio

    coro = log_and_alert(
        redis=redis, trigger=trigger, message=message,
        severity=severity, bot_id=bot_id, symbol=symbol,
        ib_order_id=ib_order_id, extra=extra, exc_info=False,
    )
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        # No running loop — close the coroutine so it doesn't leak,
        # and at least emit the log line synchronously.
        coro.close()
        _extra = {"severity": severity, "trigger": trigger,
                  "message": message, "bot_id": bot_id, "symbol": symbol,
                  "ib_order_id": ib_order_id}
        if extra:
            _extra.update(extra)
        logger.warning(json.dumps({k: v for k, v in _extra.items()
                                    if v is not None}))


async def log_and_alert(
    *,
    redis,
    trigger: str,
    message: str,
    severity: str = "WARNING",
    bot_id: str | None = None,
    symbol: str | None = None,
    ib_order_id: str | None = None,
    extra: dict[str, Any] | None = None,
    exc_info: bool = True,
) -> None:
    """Log the failure at ERROR and publish an alert to ``alerts:active``.

    Args:
        redis: async Redis client. When ``None`` the function becomes a
            logging-only shim — tests and early-boot code can still call
            it without crashing.
        trigger: stable category tag (e.g. ``ORDER_PLACE_FAILED``,
            ``SUBSCRIBE_BARS_FAILED``). Used as the ``event`` field in
            the log line and as the alert's ``trigger``.
        message: human-readable description of the failure.
        severity: ``"WARNING"`` (default) or ``"CATASTROPHIC"``. The
            CatastrophicOverlay renders the latter as a blocking modal.
        bot_id, symbol, ib_order_id: optional correlation identifiers.
        extra: any additional context to splat into the alert payload
            AND the JSON log line.
        exc_info: when True (default), ``logger.error`` attaches the
            current exception's traceback. Set to False if no exception
            is in scope.
    """
    log_payload: dict[str, Any] = {
        "event": trigger,
        "severity": severity,
        "message": message,
    }
    if bot_id:
        log_payload["bot_id"] = bot_id
    if symbol:
        log_payload["symbol"] = symbol
    if ib_order_id:
        log_payload["ib_order_id"] = ib_order_id
    if extra:
        log_payload.update(extra)
    logger.error(json.dumps(log_payload), exc_info=exc_info)

    if redis is None:
        return

    alert_id = str(uuid.uuid4())
    alert_dict: dict[str, Any] = {
        "id": alert_id,
        "severity": severity,
        "trigger": trigger,
        "message": message,
        "created_at": _now_utc_iso(),
        "bot_id": bot_id,
        "symbol": symbol,
        "ib_order_id": ib_order_id,
    }
    if extra:
        alert_dict.update(extra)

    try:
        from ib_trader.redis.state import StateKeys
        from ib_trader.redis.streams import publish_activity

        await StateKeys.publish_alert(redis, alert_id, alert_dict)
        await publish_activity(redis, "alerts")
    except Exception:
        # Alert plumbing itself failed — the original failure is already
        # on the log line above. Don't cascade.
        logger.exception(
            json.dumps({
                "event": "LOG_AND_ALERT_PUBLISH_FAILED",
                "trigger": trigger,
            })
        )
