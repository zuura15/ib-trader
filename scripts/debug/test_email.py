"""One-shot script to verify email notifications work end-to-end.

Usage (loads .env automatically):
    uv run python scripts/debug/test_email.py

Sends one entry-style email and one close-style email to whatever
EMAIL_TO is set in .env. Exits 0 if both `_send` calls returned
without exception; check your inbox to confirm delivery.
"""
import os
from pathlib import Path

# Load .env from project root.
_env_path = Path(__file__).resolve().parents[2] / ".env"
if _env_path.exists():
    for raw in _env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

from ib_trader.notifications.email_notifier import (  # noqa: E402
    send_entry_email, send_close_email,
)


def main() -> None:
    print(f"EMAIL_NOTIFICATIONS_ENABLED = {os.environ.get('EMAIL_NOTIFICATIONS_ENABLED', '<unset>')}")
    print(f"EMAIL_FROM = {os.environ.get('EMAIL_FROM', '<unset>')}")
    print(f"EMAIL_TO   = {os.environ.get('EMAIL_TO', '<unset>')}")
    print()

    print("Sending entry test email...")
    send_entry_email(
        bot_name="chart-bot-1",
        symbol="MGCQ6",
        sec_type="FUT",
        direction="long",
        qty="1",
        fill_price="4612.70",
        fill_time="2026-05-18T16:00:07+00:00",
        serial=545,
        entry_path="touch",
        marginal_filters=["shoulder", "opposing_dominance"],
    )
    print("  → sent (check inbox)")

    print()
    print("Sending close test email...")
    send_close_email(
        bot_name="chart-bot-1",
        symbol="MGCQ6",
        sec_type="FUT",
        direction="long",
        entry_price="4612.70",
        entry_qty="1",
        entry_time="2026-05-18T16:00:07+00:00",
        exit_price="4614.00",
        exit_qty="1",
        exit_time="2026-05-18T16:00:35+00:00",
        gross_pnl="13.00",
        commission="2.91",
        net_pnl="10.09",
        commission_source="transactions",
        exit_reason="trail_stop",
        duration_seconds=28,
        trail_reset_count=0,
        entry_serial=545,
        exit_serial=546,
        entry_path="touch",
        marginal_filters=["shoulder", "opposing_dominance"],
    )
    print("  → sent (check inbox)")


if __name__ == "__main__":
    main()
