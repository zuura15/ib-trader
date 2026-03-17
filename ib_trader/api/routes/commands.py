"""Command submission and status endpoints.

POST /api/commands — submit a command to the engine (returns 202)
GET /api/commands/{cmd_id} — poll for command result
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_pending_commands
from ib_trader.api.serializers import CommandRequest, CommandResponse, CommandStatusResponse
from ib_trader.data.models import PendingCommand
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository

router = APIRouter(prefix="/api/commands", tags=["commands"])


@router.post("", status_code=202, response_model=CommandResponse)
def submit_command(
    body: CommandRequest,
    repo: PendingCommandRepository = Depends(get_pending_commands),
):
    """Submit a command to the engine for execution.

    Returns 202 Accepted immediately. Poll GET /api/commands/{id}
    or subscribe to the WebSocket commands channel for the result.
    """
    cmd = PendingCommand(
        source="api",
        broker=body.broker,
        command_text=body.command,
        submitted_at=datetime.now(timezone.utc),
    )
    repo.insert(cmd)
    return CommandResponse(command_id=cmd.id, status="pending")


@router.get("/{cmd_id}", response_model=CommandStatusResponse)
def get_command_status(
    cmd_id: str,
    repo: PendingCommandRepository = Depends(get_pending_commands),
):
    """Get the current status of a submitted command."""
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
