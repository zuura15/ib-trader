"""Repository for the pending_commands queue.

Clients (REPL, API, bots) insert commands with status=PENDING.
The engine service polls for PENDING rows, executes them, and marks
them SUCCESS or FAILURE with output/error text.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import scoped_session, Session

from ib_trader.data.base import PendingCommandRepositoryBase
from ib_trader.data.models import PendingCommand, PendingCommandStatus

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class PendingCommandRepository(PendingCommandRepositoryBase):
    """SQLAlchemy repository for PendingCommand persistence."""

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def insert(self, cmd: PendingCommand) -> PendingCommand:
        """Persist a new pending command and return it."""
        s = self._session()
        s.add(cmd)
        s.commit()
        return cmd

    def get(self, cmd_id: str) -> PendingCommand | None:
        """Return the command with the given ID, or None."""
        return (
            self._session()
            .query(PendingCommand)
            .filter(PendingCommand.id == cmd_id)
            .first()
        )

    def get_pending(self) -> list[PendingCommand]:
        """Return all commands with status PENDING, ordered by submitted_at."""
        return (
            self._session()
            .query(PendingCommand)
            .filter(PendingCommand.status == PendingCommandStatus.PENDING)
            .order_by(PendingCommand.submitted_at.asc())
            .all()
        )

    def get_by_status(self, status: PendingCommandStatus) -> list[PendingCommand]:
        """Return all commands with the given status."""
        return (
            self._session()
            .query(PendingCommand)
            .filter(PendingCommand.status == status)
            .all()
        )

    def update_status(self, cmd_id: str, status: PendingCommandStatus) -> None:
        """Update the status of a command. Sets started_at when transitioning to RUNNING."""
        s = self._session()
        cmd = s.query(PendingCommand).filter(PendingCommand.id == cmd_id).first()
        if cmd is None:
            logger.warning('{"event": "CMD_NOT_FOUND", "cmd_id": "%s", "action": "update_status"}',
                           cmd_id)
            return
        cmd.status = status
        if status == PendingCommandStatus.RUNNING:
            cmd.started_at = _now_utc()
        s.commit()

    def complete(self, cmd_id: str, status: PendingCommandStatus,
                 output: str | None = None, error: str | None = None) -> None:
        """Mark a command as completed (SUCCESS or FAILURE) with output/error."""
        s = self._session()
        cmd = s.query(PendingCommand).filter(PendingCommand.id == cmd_id).first()
        if cmd is None:
            logger.warning('{"event": "CMD_NOT_FOUND", "cmd_id": "%s", "action": "complete"}',
                           cmd_id)
            return
        cmd.status = status
        cmd.output = output
        cmd.error = error
        cmd.completed_at = _now_utc()
        s.commit()

    def get_by_source(self, source: str, limit: int = 50) -> list[PendingCommand]:
        """Return recent commands from a given source, newest first."""
        return (
            self._session()
            .query(PendingCommand)
            .filter(PendingCommand.source == source)
            .order_by(PendingCommand.submitted_at.desc())
            .limit(limit)
            .all()
        )
