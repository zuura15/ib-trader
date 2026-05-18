"""Per-symbol round-trip commission expectations.

Single source of truth for two callers:

1. ``daemon.reconciler.run_commission_reconciliation`` uses these as
   the threshold for ``BotTradeRepository.find_undercommissioned_trades``
   — any closed bot_trade whose stored commission is below the symbol
   floor is a backfill candidate.

2. ``bots.runtime._handle_record_trade_closed`` uses these as the
   fallback commission when the transactions-sourced seed is 0 / None
   at audit-row emission time. Lets the operator see a sensible net
   P&L on the TRADE_CLOSED row immediately, with a payload tag
   distinguishing the source.

Values are observed IBKR commissions for micro futures (typical
account, including exchange/regulatory fees). Update if your
account's effective rate changes; missing symbols fall back to 0 —
i.e. no threshold check and gross P&L equals net.
"""
from __future__ import annotations

from decimal import Decimal

# Round-trip commission per contract — entry leg + exit leg combined.
ROUND_TRIP_MIN: dict[str, Decimal] = {
    # Micro Gold @ ~$0.97/side.
    "MGCM6": Decimal("1.94"),
    # Micro S&P 500 @ ~$0.62/side.
    "MESM6": Decimal("1.24"),
    # Micro Nasdaq @ ~$0.62/side.
    "MNQM6": Decimal("1.24"),
}


def expected_min(symbol: str) -> Decimal:
    """Return the round-trip commission floor for ``symbol``. Unknown
    symbols return Decimal(0) so the caller's threshold check is a
    no-op (instead of raising) and gross == net by default."""
    return ROUND_TRIP_MIN.get(symbol, Decimal("0"))
