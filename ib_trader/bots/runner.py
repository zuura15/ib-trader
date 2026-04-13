"""Bot runner — manages bot lifecycle as a separate process.

The bot runner polls the bots table for status changes and manages
each bot as an asyncio task. It communicates with the engine ONLY
through SQLite (pending_commands table).

Zero memory state: on startup, checks for RUNNING bots and restarts them.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import scoped_session

from ib_trader.data.models import BotStatus, BotEvent
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.bots.base import BotBase
from ib_trader.bots.registry import get_strategy_class

logger = logging.getLogger(__name__)

_RUNNER_POLL_INTERVAL = 1.0  # seconds between bot table polls


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _run_single_bot(bot_row, session_factory: scoped_session,
                           recover: bool = False,
                           redis=None, engine_url: str | None = None) -> None:
    """Run a single bot's tick loop until stopped or crashed.

    Args:
        bot_row: Bot model instance from SQLite.
        session_factory: For creating repositories.
        recover: If True, call on_startup with open positions.
    """
    strategy_cls = get_strategy_class(bot_row.strategy)
    if strategy_cls is None:
        print(f"[BOTS] ERROR  Unknown strategy '{bot_row.strategy}' for bot '{bot_row.name}'")
        logger.error(json.dumps({
            "event": "UNKNOWN_STRATEGY",
            "bot_id": bot_row.id,
            "strategy": bot_row.strategy,
        }))
        bots_repo = BotRepository(session_factory)
        bots_repo.update_status(
            bot_row.id, BotStatus.ERROR,
            error_message=f"Unknown strategy: {bot_row.strategy}",
        )
        return

    config = json.loads(bot_row.config_json) if bot_row.config_json else {}
    config["tick_interval_seconds"] = bot_row.tick_interval_seconds
    config["_redis"] = redis
    config["_engine_url"] = engine_url
    bot = strategy_cls(bot_row.id, config, session_factory)

    events_repo = BotEventRepository(session_factory)
    bots_repo = BotRepository(session_factory)

    # Log start event
    events_repo.insert(BotEvent(
        bot_id=bot_row.id, event_type="STARTED",
        message=f"Bot started: {bot_row.name} ({bot_row.strategy})",
        recorded_at=_now_utc(),
    ))

    # Crash recovery: pass open positions from previous incarnation
    if recover:
        pending_repo = PendingCommandRepository(session_factory)
        from ib_trader.data.repository import TradeRepository
        trades_repo = TradeRepository(session_factory)
        open_positions = trades_repo.get_open()
        # Filter to positions opened by this bot
        bot_source = f"bot:{bot_row.id}"
        bot_cmds = pending_repo.get_by_source(bot_source, limit=200)
        # Simple heuristic: pass all open positions (bot can filter further)
        await bot.on_startup(open_positions)

    try:
        while True:
            # Check if bot should still be running
            current = bots_repo.get(bot_row.id)
            if current is None or current.status != BotStatus.RUNNING:
                break

            # Update heartbeat
            bot.update_heartbeat()

            # Execute tick
            try:
                await bot.on_tick()
            except Exception as tick_error:
                print(f"[BOTS] ERROR  Bot '{bot_row.name}' tick failed: {tick_error}")
                logger.exception(json.dumps({
                    "event": "BOT_TICK_ERROR",
                    "bot_id": bot_row.id,
                    "error": str(tick_error),
                }))
                events_repo.insert(BotEvent(
                    bot_id=bot_row.id, event_type="ERROR",
                    message=str(tick_error),
                    recorded_at=_now_utc(),
                ))
                bots_repo.update_status(
                    bot_row.id, BotStatus.ERROR,
                    error_message=str(tick_error),
                )
                return

            await asyncio.sleep(bot.tick_interval)

    except asyncio.CancelledError:
        pass
    finally:
        try:
            await bot.on_stop()
        except Exception:
            logger.exception(json.dumps({
                "event": "BOT_STOP_ERROR", "bot_id": bot_row.id,
            }))
        events_repo.insert(BotEvent(
            bot_id=bot_row.id, event_type="STOPPED",
            message=f"Bot stopped: {bot_row.name}",
            recorded_at=_now_utc(),
        ))


async def run_bot_runner(session_factory: scoped_session,
                         redis=None, engine_url: str | None = None) -> None:
    """Main bot runner loop. Manages bot lifecycle as asyncio tasks.

    Polls the bots table every second for status changes.
    Starts/stops bot tasks accordingly.
    """
    bots_repo = BotRepository(session_factory)
    running_tasks: dict[str, asyncio.Task] = {}

    logger.info(json.dumps({"event": "BOT_RUNNER_STARTED"}))

    while True:
        try:
            # FIRST: clean up finished/crashed tasks before checking for new ones.
            # This prevents the race where a crashed bot is still in running_tasks
            # when the DB shows RUNNING (from a concurrent /start request).
            for bot_id in list(running_tasks.keys()):
                task = running_tasks[bot_id]
                if task.done():
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        bots_repo.update_status(
                            bot_id, BotStatus.ERROR,
                            error_message=str(exc),
                        )
                        print(f"[BOTS] CRASH  bot_id={bot_id}: {exc}")
                        logger.error(json.dumps({
                            "event": "BOT_TASK_CRASHED",
                            "bot_id": bot_id,
                            "error": str(exc),
                        }))
                    del running_tasks[bot_id]

            all_bots = bots_repo.get_all()
            for bot_row in all_bots:
                if bot_row.status == BotStatus.RUNNING and bot_row.id not in running_tasks:
                    # Guard: verify status is still RUNNING (not changed by concurrent request)
                    fresh = bots_repo.get(bot_row.id)
                    if fresh is None or fresh.status != BotStatus.RUNNING:
                        continue

                    task = asyncio.create_task(
                        _run_single_bot(bot_row, session_factory, recover=True,
                                        redis=redis, engine_url=engine_url),
                    )
                    running_tasks[bot_row.id] = task
                    print(f"[BOTS] START  '{bot_row.name}' (strategy={bot_row.strategy}, tick={bot_row.tick_interval_seconds}s)")
                    logger.info(json.dumps({
                        "event": "BOT_TASK_STARTED",
                        "bot_id": bot_row.id,
                        "name": bot_row.name,
                    }))

                elif bot_row.status != BotStatus.RUNNING and bot_row.id in running_tasks:
                    # Stop bot
                    running_tasks[bot_row.id].cancel()
                    del running_tasks[bot_row.id]
                    print(f"[BOTS] STOP   '{bot_row.name}'")
                    logger.info(json.dumps({
                        "event": "BOT_TASK_STOPPED",
                        "bot_id": bot_row.id,
                    }))

        except Exception:
            logger.exception(json.dumps({"event": "BOT_RUNNER_POLL_ERROR"}))

        await asyncio.sleep(_RUNNER_POLL_INTERVAL)
