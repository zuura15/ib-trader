"""Command submission and status endpoints.

POST /api/commands — forwards command to engine HTTP API (synchronous)
GET /api/commands/{cmd_id} — get command result from audit log
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_pending_commands
from ib_trader.api.serializers import CommandRequest, CommandResponse, CommandStatusResponse
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.config.loader import load_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/commands", tags=["commands"])


@router.post("", status_code=202, response_model=CommandResponse)
async def submit_command(body: CommandRequest):
    """Submit a command to the engine via its HTTP API.

    Forwards to the engine's internal API for immediate execution.
    No polling — the engine processes synchronously and returns the result.
    """
    # SAFETY: never forward in test environments
    import os
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TESTING"):
        raise HTTPException(status_code=503, detail="Command forwarding disabled in test environment")

    settings = load_settings("config/settings.yaml")
    engine_port = settings.get("engine_internal_port", 8081)
    engine_url = f"http://127.0.0.1:{engine_port}"

    cmd_text = body.command.strip()
    parts = cmd_text.split()
    verb = parts[0].lower() if parts else ""

    # Read-only commands — handle locally, no engine forwarding
    if verb in ("orders", "status", "stats", "help"):
        from ib_trader.api.deps import get_session_factory
        sf = get_session_factory()
        try:
            if verb == "orders":
                from ib_trader.data.repositories.transaction_repository import TransactionRepository
                open_orders = TransactionRepository(sf).get_open_orders()
                output = "\n".join(
                    f"  #{o.trade_serial or '-':>3} {o.symbol:5} {o.side:4} @ {o.limit_price or 'MKT'}"
                    for o in open_orders
                ) if open_orders else "No open orders."
            elif verb in ("status", "stats"):
                from ib_trader.data.repository import TradeRepository
                all_trades = TradeRepository(sf).get_all()
                closed = [t for t in all_trades if t.status.value == "CLOSED" and t.realized_pnl is not None]
                total_pnl = sum(float(t.realized_pnl) for t in closed)
                output = f"Trades: {len(all_trades)} total ({len(closed)} closed), P&L: ${total_pnl:+.2f}"
            else:
                output = "Commands: buy, sell, close, orders, status, help"
            return CommandResponse(command_id="local", status="completed", output=output)
        finally:
            sf.remove()

    try:
        # Must exceed the engine's internal wait windows. Market / bid-ask
        # orders block for up to ~30s waiting on the fill_event; mid orders
        # can run the full reprice_duration_seconds (~10s by default) plus
        # overhead. 120s gives headroom without being an unbounded hang.
        async with httpx.AsyncClient(timeout=120) as client:
            if verb in ("buy", "sell"):
                symbol = parts[1] if len(parts) > 1 else ""
                qty = parts[2] if len(parts) > 2 else "1"
                order_type = parts[3] if len(parts) > 3 else "mid"
                side = "BUY" if verb == "buy" else "SELL"

                resp = await client.post(f"{engine_url}/engine/orders", json={
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "order_type": order_type,
                    "cmd_id": body.command_id,
                })
            elif verb == "close":
                serial = int(parts[1]) if len(parts) > 1 else 0
                strategy = parts[2] if len(parts) > 2 else "market"
                resp = await client.post(f"{engine_url}/engine/close", json={
                    "serial": serial,
                    "strategy": strategy,
                    "cmd_id": body.command_id,
                })
            else:
                raise HTTPException(status_code=400, detail=f"Unknown command: {verb}")

            if resp.status_code == 200:
                result = resp.json()
                # Use the engine's rendered output (includes fill details)
                # instead of constructing a static "Order #X placed" string.
                output = result.get("output") or ""
                if not output:
                    # Fallback for engines that don't return output yet
                    if result.get("serial"):
                        output = f"Order #{result['serial']} placed"
                    if result.get("order_ref"):
                        output += f" ({result['order_ref']})"
                # Prefer the cmd_id echoed back by the engine (matches the
                # Redis output stream the client has already subscribed to);
                # fall back to the client-supplied id, then legacy fields.
                cmd_id = (
                    result.get("cmd_id")
                    or body.command_id
                    or result.get("ib_order_id")
                    or "completed"
                )
                return CommandResponse(
                    command_id=cmd_id,
                    status="completed",
                    output=output or "Order accepted",
                )
            else:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)

    except httpx.ConnectError as e:
        raise HTTPException(
            status_code=503,
            detail="Engine not reachable. Is ib-engine running?",
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception('{"event": "COMMAND_FORWARD_FAILED"}')
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{cmd_id}", response_model=CommandStatusResponse)
def get_command_status(
    cmd_id: str,
    repo: PendingCommandRepository = Depends(get_pending_commands),
):
    """Get the current status of a submitted command from the audit log."""
    cmd = repo.get(cmd_id)
    if cmd is None:
        raise HTTPException(status_code=404, detail="Command not found")
    return CommandStatusResponse(
        command_id=cmd.id,
        status=cmd.status.value,
        command_text=cmd.command_text,
        source=cmd.source,
        output=cmd.output,
        error=cmd.error,
        submitted_at=cmd.submitted_at,
        started_at=cmd.started_at,
        completed_at=cmd.completed_at,
    )
