"""Redis-backed read/write helpers for bot runtime state.

Replaces every site that used to touch the SQLite ``bots`` table for
ephemeral state (status, heartbeat, last_action, error_message) and
the hot-path KILL_SWITCH check.

Keys + TTLs are defined on ``ib_trader.redis.state.StateKeys``. This
module adds the serialization layer and fail-closed semantics the
runtime expects.

Fail-closed rule
----------------
``is_kill_switch_engaged`` returns True when Redis is unreachable or
the read raises. BUY gating reads this; it must err on the side of
rejecting trades rather than silently allowing them when the control
plane is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from ib_trader.redis.state import StateKeys, StateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BotStatus helpers (string constants — avoids importing SQLAlchemy enum)
# ---------------------------------------------------------------------------

STATUS_RUNNING = "RUNNING"
STATUS_STOPPED = "STOPPED"
STATUS_ERROR = "ERROR"
STATUS_PAUSED = "PAUSED"
VALID_STATUSES = frozenset({STATUS_RUNNING, STATUS_STOPPED, STATUS_ERROR, STATUS_PAUSED})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BotStateStore:
    """Thin wrapper over ``StateStore`` for bot runtime state.

    Every method is a no-op when ``redis`` is None so the runner can
    keep running in dev/test setups without Redis — with the one
    exception of ``is_kill_switch_engaged`` which treats "no Redis" as
    the safety-critical denial case, matching the fail-closed rule.
    """

    def __init__(self, redis) -> None:
        self._redis = redis
        self._store = StateStore(redis) if redis is not None else None

    # ---- status ----

    async def set_status(
        self, bot_id: str, status: str, *, error_message: str | None = None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid bot status: {status!r}")
        if self._store is None:
            return
        await self._store.set(StateKeys.bot_status(bot_id), {"status": status, "ts": _now_iso()})
        if status == STATUS_ERROR and error_message:
            await self._store.set(
                StateKeys.bot_error_message(bot_id),
                {"message": error_message, "ts": _now_iso()},
                ttl=StateKeys.BOT_ERROR_MESSAGE_TTL,
            )
        elif status in (STATUS_RUNNING, STATUS_STOPPED):
            # Clear stale error when the bot transitions back to a healthy state.
            await self._store.delete(StateKeys.bot_error_message(bot_id))

    async def get_status(self, bot_id: str) -> str:
        """Return RUNNING/STOPPED/etc, defaulting to STOPPED when unset."""
        if self._store is None:
            return STATUS_STOPPED
        data = await self._store.get(StateKeys.bot_status(bot_id))
        if not data:
            return STATUS_STOPPED
        return str(data.get("status") or STATUS_STOPPED)

    async def get_error_message(self, bot_id: str) -> str | None:
        if self._store is None:
            return None
        data = await self._store.get(StateKeys.bot_error_message(bot_id))
        if not data:
            return None
        return data.get("message")

    # ---- heartbeat ----

    async def update_heartbeat(self, bot_id: str) -> None:
        if self._store is None:
            return
        await self._store.set(
            StateKeys.bot_heartbeat(bot_id),
            {"ts": _now_iso()},
            ttl=StateKeys.BOT_HEARTBEAT_TTL,
        )

    async def get_heartbeat(self, bot_id: str) -> str | None:
        if self._store is None:
            return None
        data = await self._store.get(StateKeys.bot_heartbeat(bot_id))
        if not data:
            return None
        return data.get("ts")

    # ---- last action ----

    async def set_last_action(self, bot_id: str, action: str) -> None:
        if self._store is None:
            return
        await self._store.set(
            StateKeys.bot_last_action(bot_id),
            {"action": action, "ts": _now_iso()},
            ttl=StateKeys.BOT_LAST_ACTION_TTL,
        )

    async def clear_last_action(self, bot_id: str) -> None:
        if self._store is None:
            return
        await self._store.delete(StateKeys.bot_last_action(bot_id))

    async def get_last_action(self, bot_id: str) -> dict | None:
        if self._store is None:
            return None
        return await self._store.get(StateKeys.bot_last_action(bot_id))

    # ---- kill switch (safety-critical) ----

    async def engage_kill_switch(self, bot_id: str, reason: str = "") -> None:
        if self._store is None:
            logger.error(
                '{"event": "KILL_SWITCH_ENGAGE_NO_REDIS", "bot_id": "%s"}', bot_id,
            )
            return
        await self._store.set(
            StateKeys.bot_kill_switch(bot_id),
            {"engaged": True, "ts": _now_iso(), "reason": reason},
        )

    async def release_kill_switch(self, bot_id: str) -> None:
        if self._store is None:
            return
        await self._store.delete(StateKeys.bot_kill_switch(bot_id))

    async def is_kill_switch_engaged(self, bot_id: str) -> bool:
        """Fail-closed: unreachable Redis or read error → engaged (True).

        The RiskMiddleware blocks BUYs on True. Treating "no answer" as
        "engaged" is the intentional safety default: we'd rather refuse
        trading than silently let orders through when the control plane
        is down. Callers must not suppress False here without explicit
        knowledge that the kill switch really is cleared.
        """
        if self._store is None:
            # No Redis configured → BUY gating fails closed.
            return True
        try:
            data = await self._store.get(StateKeys.bot_kill_switch(bot_id))
        except Exception:
            logger.exception(
                '{"event": "KILL_SWITCH_READ_FAILED_FAILING_CLOSED", "bot_id": "%s"}',
                bot_id,
            )
            return True
        return bool(data) and bool(data.get("engaged"))

    # ---- bulk read (used by API list / WS snapshot) ----

    async def snapshot_runtime_state(
        self, bot_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """Return ``{bot_id: {status, heartbeat, last_action, error_message, kill_switch}}``.

        One round trip per field per bot — fine for the low cardinalities
        (tens of bots max) the UI deals with. Defaults are filled in so
        the caller can compose a full response without None-checking.
        """
        out: dict[str, dict[str, Any]] = {}
        if self._store is None:
            for bot_id in bot_ids:
                out[bot_id] = {
                    "status": STATUS_STOPPED,
                    "heartbeat": None,
                    "last_action": None,
                    "error_message": None,
                    "kill_switch": False,
                }
            return out

        # Fan out reads concurrently per bot.
        async def _read_one(bot_id: str) -> tuple[str, dict[str, Any]]:
            status_task = self._store.get(StateKeys.bot_status(bot_id))
            hb_task = self._store.get(StateKeys.bot_heartbeat(bot_id))
            last_task = self._store.get(StateKeys.bot_last_action(bot_id))
            err_task = self._store.get(StateKeys.bot_error_message(bot_id))
            ks_task = self._store.get(StateKeys.bot_kill_switch(bot_id))
            status, hb, last, err, ks = await asyncio.gather(
                status_task, hb_task, last_task, err_task, ks_task,
            )
            return bot_id, {
                "status": (status or {}).get("status") or STATUS_STOPPED,
                "heartbeat": (hb or {}).get("ts"),
                "last_action": last,
                "error_message": (err or {}).get("message"),
                "kill_switch": bool((ks or {}).get("engaged")),
            }

        pairs = await asyncio.gather(*[_read_one(b) for b in bot_ids])
        for bot_id, state in pairs:
            out[bot_id] = state
        return out
