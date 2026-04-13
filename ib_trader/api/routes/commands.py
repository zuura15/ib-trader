"""Command submission and status endpoints.

POST /api/commands — forwards command to engine HTTP API (synchronous)
GET /api/commands/{cmd_id} — get command result from audit log
"""
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_pending_commands, get_session_factory
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
    settings = load_settings("config/settings.yaml")
    engine_port = settings.get("engine_internal_port", 8081)
    engine_url = f"http://127.0.0.1:{engine_port}"

    cmd_text = body.command.strip()
    parts = cmd_text.split()
    verb = parts[0].lower() if parts else ""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if verb in ("buy", "sell"):
                # Parse into structured request
                symbol = parts[1] if len(parts) > 1 else ""
                qty = parts[2] if len(parts) > 2 else "1"
                order_type = parts[3] if len(parts) > 3 else "mid"
                side = "BUY" if verb == "buy" else "SELL"

                resp = await client.post(f"{engine_url}/engine/orders", json={
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "order_type": order_type,
                })
            elif verb == "close":
                serial = int(parts[1]) if len(parts) > 1 else 0
                strategy = parts[2] if len(parts) > 2 else "market"
                resp = await client.post(f"{engine_url}/engine/close", json={
                    "serial": serial,
                    "strategy": strategy,
                })
            else:
                # For non-order commands (status, orders, help), still use
                # the engine's execute_single_command via a generic endpoint
                resp = await client.post(f"{engine_url}/engine/orders", json={
                    "symbol": "",
                    "side": "BUY",
                    "qty": "0",
                    "order_type": cmd_text,
                })

            if resp.status_code == 200:
                result = resp.json()
                return CommandResponse(
                    command_id=result.get("ib_order_id", ""),
                    status="completed",
                )
            else:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)

    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="Engine not reachable. Is ib-engine running?",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception('{"event": "COMMAND_FORWARD_FAILED"}')
        raise HTTPException(status_code=500, detail=str(e))


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
