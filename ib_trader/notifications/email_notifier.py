"""SMTP email notifications for bot fills.

Two entry points:

- ``send_entry_email(...)`` — fired when an entry order is placed
  successfully (after IB confirms placement).
- ``send_close_email(...)`` — fired when a position is closed,
  mirroring the BotTradesPanel / TRADE_CLOSED detail view.

All SMTP config comes from environment variables. When
``EMAIL_NOTIFICATIONS_ENABLED`` is not ``"true"`` (case-insensitive)
the functions are no-ops. Failure to send is logged at WARNING
and swallowed — never blocks the trading path. The password is
NEVER logged at any level.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get(
        "EMAIL_NOTIFICATIONS_ENABLED", "false"
    ).strip().lower() == "true"


def _smtp_config() -> dict[str, str] | None:
    """Read SMTP config from env. Returns None when a required key
    is missing. Caller treats None as "skip sending."""
    keys = (
        "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT",
        "EMAIL_SMTP_USER", "EMAIL_SMTP_PASSWORD",
        "EMAIL_FROM", "EMAIL_TO",
    )
    cfg: dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k, "").strip()
        if not v:
            return None
        cfg[k] = v
    return cfg


def _send(subject: str, body: str) -> None:
    """Send a plain-text email. No-op when disabled or unconfigured.
    Errors caught and logged WARNING with no credential exposure."""
    if not _enabled():
        return
    cfg = _smtp_config()
    if cfg is None:
        logger.warning(
            '{"event": "EMAIL_NOT_SENT", "reason": "missing_smtp_config"}'
        )
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["EMAIL_FROM"]
    msg["To"] = cfg["EMAIL_TO"]
    msg.set_content(body)
    try:
        port = int(cfg["EMAIL_SMTP_PORT"])
        # Gmail SMTP on port 587 uses STARTTLS. Port 465 uses
        # SMTP_SSL. Default behavior: STARTTLS for 587, implicit
        # TLS for 465.
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                cfg["EMAIL_SMTP_HOST"], port, context=ctx, timeout=15,
            ) as s:
                s.login(cfg["EMAIL_SMTP_USER"], cfg["EMAIL_SMTP_PASSWORD"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(
                cfg["EMAIL_SMTP_HOST"], port, timeout=15,
            ) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                s.login(cfg["EMAIL_SMTP_USER"], cfg["EMAIL_SMTP_PASSWORD"])
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        logger.warning(
            '{"event": "EMAIL_AUTH_FAILED", "code": %d, '
            '"detail": "check EMAIL_SMTP_USER / app password"}',
            getattr(e, "smtp_code", 0),
        )
    except Exception as e:  # noqa: BLE001 — broad on purpose
        logger.warning(
            '{"event": "EMAIL_SEND_FAILED", "error": "%s"}',
            type(e).__name__,
        )


def _fmt_money(v: Any) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):.2f}"


def _signed_money(v: Any) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    sign = "+" if n >= 0 else "-"
    return f"{sign}${abs(n):.2f}"


def send_entry_email(
    *,
    bot_name: str | None,
    symbol: str,
    sec_type: str,
    direction: str,
    qty: Any,
    fill_price: Any,
    fill_time: str,
    serial: Any,
    entry_path: str | None = None,
    marginal_filters: list[str] | None = None,
) -> None:
    """Email the operator when an entry order has been placed/filled."""
    dir_up = direction.upper()
    subject = (
        f"[ib-trader] {symbol} {dir_up} entry @ "
        f"{_fmt_money(fill_price)} × {qty}"
    )
    lines = [
        f"Bot:         {bot_name or '—'}",
        f"Symbol:      {symbol} ({sec_type})",
        f"Direction:   {dir_up}",
        f"Qty:         {qty}",
        f"Fill price:  {_fmt_money(fill_price)}",
        f"Time:        {fill_time}",
        f"Serial:      #{serial}",
    ]
    if entry_path:
        lines.append(f"Entry path:  {entry_path}")
    if marginal_filters:
        lines.append(
            f"  bypassed:  {', '.join(marginal_filters)}"
        )
    _send(subject, "\n".join(lines))


def send_close_email(
    *,
    bot_name: str | None,
    symbol: str,
    sec_type: str,
    direction: str,
    entry_price: Any,
    entry_qty: Any,
    entry_time: str | None,
    exit_price: Any,
    exit_qty: Any,
    exit_time: str | None,
    gross_pnl: Any,
    commission: Any,
    net_pnl: Any,
    commission_source: str | None,
    exit_reason: str | None,
    duration_seconds: Any,
    trail_reset_count: Any,
    entry_serial: Any,
    exit_serial: Any,
    entry_path: str | None = None,
    marginal_filters: list[str] | None = None,
) -> None:
    """Email the operator when a position is closed.

    Mirrors the audit feed's TRADE_CLOSED detail view: entry / exit /
    P&L breakdown / direction · reason / duration · trail resets /
    entry tag · bypassed filters / serials.
    """
    dir_up = direction.upper()
    net_str = _signed_money(net_pnl)
    star = "★" if (net_pnl is not None and float(net_pnl) >= 0) else "✗"
    reason = exit_reason or "—"
    subject = (
        f"[ib-trader] {symbol} {dir_up} closed {star} {net_str} "
        f"({reason})"
    )
    dur = (f"{int(duration_seconds)}s"
           if duration_seconds is not None else "—")
    resets = str(trail_reset_count if trail_reset_count is not None else "0")
    comm_note = (
        " (est. — backfill pending)"
        if commission_source == "fallback_standard" else ""
    )
    lines = [
        f"Bot:         {bot_name or '—'}",
        f"Symbol:      {symbol} ({sec_type})",
        f"Direction:   {dir_up}",
        f"Result:      {star} {net_str} (Net)",
        "",
        f"Entry:       {_fmt_money(entry_price)} × {entry_qty}"
        f"   @ {entry_time or '—'}",
        f"Exit:        {_fmt_money(exit_price)} × {exit_qty}"
        f"   @ {exit_time or '—'}",
        f"Duration:    {dur}",
        f"Trail resets: {resets}",
        "",
        "P&L breakdown:",
        f"  Gross:      {_signed_money(gross_pnl)}",
        f"  Commission: -{_fmt_money(commission)}{comm_note}",
        f"  Net:        {net_str}",
        "",
        f"Exit reason: {reason}",
        f"Serials:     #{entry_serial} → #{exit_serial}",
    ]
    if entry_path:
        path_line = f"Entry path:  {entry_path}"
        if marginal_filters:
            path_line += f" (bypassed: {', '.join(marginal_filters)})"
        lines.append("")
        lines.append(path_line)
    _send(subject, "\n".join(lines))
