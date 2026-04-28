"""Bot trades endpoint.

GET /api/bot-trades — list synthesized bot entry-to-exit round-trips
(optionally filtered by bot_id).
"""

from fastapi import APIRouter, Depends, Query

from ib_trader.api.deps import get_bot_trades
from ib_trader.api.serializers import BotTradeResponse
from ib_trader.data.repositories.bot_trade_repository import BotTradeRepository

router = APIRouter(prefix="/api/bot-trades", tags=["bot-trades"])


def _serialize(t) -> BotTradeResponse:
    return BotTradeResponse(
        id=t.id,
        bot_id=t.bot_id,
        bot_name=t.bot_name,
        symbol=t.symbol,
        direction=t.direction,
        entry_price=str(t.entry_price),
        entry_qty=str(t.entry_qty),
        entry_time=t.entry_time,
        exit_price=str(t.exit_price) if t.exit_price is not None else None,
        exit_qty=str(t.exit_qty) if t.exit_qty is not None else None,
        exit_time=t.exit_time,
        realized_pnl=str(t.realized_pnl) if t.realized_pnl is not None else None,
        commission=str(t.commission) if t.commission is not None else None,
        trail_reset_count=int(t.trail_reset_count or 0),
        duration_seconds=int(t.duration_seconds) if t.duration_seconds is not None else None,
        entry_serial=t.entry_serial,
        exit_serial=t.exit_serial,
        created_at=t.created_at,
    )


@router.get("", response_model=list[BotTradeResponse])
def list_bot_trades(
    bot_id: str | None = Query(None, description="Filter to one bot"),
    limit: int = Query(500, ge=1, le=2000),
    repo: BotTradeRepository = Depends(get_bot_trades),
):
    """List bot trades, most-recent-first, optionally scoped to one bot."""
    rows = repo.list_for_bot(bot_id, limit) if bot_id else repo.list_all(limit)
    return [_serialize(t) for t in rows]
