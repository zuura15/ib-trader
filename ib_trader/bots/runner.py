"""Bot runner — manages bot lifecycle via Redis streams.

The bot runner listens on the bot:control:* stream for START/STOP/FORCE_BUY
commands. No polling — all lifecycle changes are event-driven via XREAD BLOCK.

On startup, checks the bots table for any bots marked RUNNING and restarts them.
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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _run_single_bot(bot_row, session_factory: scoped_session,
                           recover: bool = False,
                           redis=None, engine_url: str | None = None) -> None:
    """Run a single bot until stopped via control stream or crash.

    The bot's on_tick() is called in a loop, but the loop is driven by
    stream events (quotes, bars) rather than a fixed timer. Between events,
    the bot also listens on its control stream for STOP/FORCE_BUY.
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

    async def _nudge(channel: str) -> None:
        if redis is None:
            return
        from ib_trader.redis.streams import publish_activity
        await publish_activity(redis, channel)

    # Log start event
    events_repo.insert(BotEvent(
        bot_id=bot_row.id, event_type="STARTED",
        message=f"Bot started: {bot_row.name} ({bot_row.strategy})",
        recorded_at=_now_utc(),
    ))
    await _nudge("bot_events")
    await _nudge("bots")

    # Crash recovery: pass open positions from previous incarnation
    if recover:
        from ib_trader.data.repository import TradeRepository
        trades_repo = TradeRepository(session_factory)
        open_positions = trades_repo.get_open()
        await bot.on_startup(open_positions)

    try:
        # Run the bot with stream-driven ticks + control stream monitoring
        await _stream_driven_loop(bot, bot_row, redis, bots_repo, events_repo)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[BOTS] ERROR  Bot '{bot_row.name}' failed: {e}")
        logger.exception(json.dumps({
            "event": "BOT_ERROR",
            "bot_id": bot_row.id,
            "error": str(e),
        }))
        events_repo.insert(BotEvent(
            bot_id=bot_row.id, event_type="ERROR",
            message=str(e),
            recorded_at=_now_utc(),
        ))
        bots_repo.update_status(
            bot_row.id, BotStatus.ERROR,
            error_message=str(e),
        )
        await _nudge("bot_events")
        await _nudge("bots")
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
        await _nudge("bot_events")
        await _nudge("bots")


async def _stream_driven_loop(bot, bot_row, redis, bots_repo, events_repo) -> None:
    """Run the bot as parallel asyncio tasks, all event-driven.

    Spawns:
      - bot.run_event_loop()       — strategy events from quote/bar/fill/position streams
      - _control_consumer()        — STOP / FORCE_BUY from control streams
      - _supervisory_loop()        — heartbeat (30s) + entry timeout + stale quote checks

    Returns when the control consumer receives STOP, when any task fails,
    or when the parent task is cancelled. The finally block in
    _run_single_bot handles cleanup.
    """
    if not redis:
        raise RuntimeError("Redis required for event-driven bot runner")

    stop_event = asyncio.Event()

    event_task = asyncio.create_task(
        bot.run_event_loop(),
        name=f"bot-events-{bot_row.id}",
    )
    control_task = asyncio.create_task(
        _control_consumer(bot, bot_row, redis, stop_event),
        name=f"bot-control-{bot_row.id}",
    )
    supervisory_task = asyncio.create_task(
        _supervisory_loop(bot, bot_row, stop_event),
        name=f"bot-supervisor-{bot_row.id}",
    )

    tasks = [event_task, control_task, supervisory_task]
    try:
        # Wait for any task to finish (STOP signal, error, or cancellation)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Surface exceptions from completed tasks (so the caller sees them)
        for t in done:
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    raise exc
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _control_consumer(bot, bot_row, redis, stop_event: asyncio.Event) -> None:
    """Listen on the bot's control stream for STOP / FORCE_BUY events.

    Also polls the bot:{id}:force_buy fallback key every loop iteration
    in case the stream event was missed (e.g., bot wasn't listening yet
    when the API published).
    """
    from ib_trader.redis.streams import StreamNames
    control_stream = StreamNames.bot_control(bot_row.id)
    control_last_id = "$"
    force_key = f"bot:{bot_row.id}:force_buy"

    while True:
        try:
            results = await redis.xread({control_stream: control_last_id}, block=5000)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('{"event": "CONTROL_STREAM_READ_ERROR"}')
            await asyncio.sleep(1)
            continue

        if results:
            for stream_name, entries in results:
                for entry_id, data in entries:
                    control_last_id = entry_id
                    action = data.get("action", "")
                    if action == "STOP":
                        logger.info(
                            '{"event": "BOT_STOP_VIA_STREAM", "bot_id": "%s"}',
                            bot_row.id,
                        )
                        stop_event.set()
                        return
                    elif action == "FORCE_BUY":
                        logger.info(
                            '{"event": "FORCE_BUY_VIA_STREAM", "bot_id": "%s"}',
                            bot_row.id,
                        )
                        bot.update_action("FORCE_BUY")
                        await bot.check_force_buy()

        # Fallback: check the force-buy key (persists even if we missed
        # the stream event during startup).
        try:
            if await redis.get(force_key):
                await redis.delete(force_key)
                logger.info(
                    '{"event": "FORCE_BUY_VIA_KEY", "bot_id": "%s"}',
                    bot_row.id,
                )
                bot.update_action("FORCE_BUY")
                await bot.check_force_buy()
        except Exception:
            pass


async def _supervisory_loop(bot, bot_row, stop_event: asyncio.Event) -> None:
    """Periodic supervisory tasks — NOT driven by market events.

    These run on their own intervals because they're time-based:
      - Heartbeat (30s): updates bots.last_heartbeat for UI liveness
      - Entry timeout: cancels stuck ENTERING positions after entry_timeout_seconds
      - Stale quote check: warns/halts when no quotes arrive for too long
    """
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10)
            return  # stop signalled
        except asyncio.TimeoutError:
            pass

        try:
            bot.update_heartbeat()
            await bot.check_entry_timeout()
            await bot.check_stale_quote()
        except Exception:
            logger.exception(
                '{"event": "SUPERVISORY_ERROR", "bot_id": "%s"}', bot_row.id,
            )


async def run_bot_runner(session_factory: scoped_session,
                         redis=None, engine_url: str | None = None) -> None:
    """Main bot runner — listens for lifecycle events via Redis streams.

    On startup, checks for RUNNING bots and restarts them.
    Then listens on bot:control:* for START/STOP commands.
    """
    from ib_trader.redis.streams import StreamNames

    bots_repo = BotRepository(session_factory)
    running_tasks: dict[str, asyncio.Task] = {}

    logger.info(json.dumps({"event": "BOT_RUNNER_STARTED"}))

    # Startup: restart any bots that were RUNNING before crash
    all_bots = bots_repo.get_all()
    for bot_row in all_bots:
        if bot_row.status == BotStatus.RUNNING:
            task = asyncio.create_task(
                _run_single_bot(bot_row, session_factory, recover=True,
                                redis=redis, engine_url=engine_url),
            )
            running_tasks[bot_row.id] = task
            print(f"[BOTS] START  '{bot_row.name}' (strategy={bot_row.strategy})")
            logger.info(json.dumps({
                "event": "BOT_TASK_STARTED",
                "bot_id": bot_row.id,
                "name": bot_row.name,
            }))

    # Listen for lifecycle commands on a global control stream
    # The API publishes START/STOP to bot:control:{bot_id}
    # We listen on all bot control streams
    global_control = "bot:control:global"
    last_id = "$"

    while True:
        try:
            # Clean up finished tasks
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
                    del running_tasks[bot_id]

            # XREAD BLOCK on global control stream — wakes on any bot lifecycle event
            results = None
            if redis:
                try:
                    results = await redis.xread(
                        {global_control: last_id},
                        block=5000,  # 5s timeout for cleanup checks
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception('{"event": "GLOBAL_CONTROL_READ_ERROR"}')
                    await asyncio.sleep(1)
                    continue
            else:
                await asyncio.sleep(5)

            if results:
                for stream_name, entries in results:
                    for entry_id, data in entries:
                        last_id = entry_id
                        action = data.get("action", "")
                        bot_id = data.get("bot_id", "")

                        if action == "START" and bot_id and bot_id not in running_tasks:
                            bot_row = bots_repo.get(bot_id)
                            if bot_row and bot_row.status == BotStatus.RUNNING:
                                task = asyncio.create_task(
                                    _run_single_bot(bot_row, session_factory, recover=True,
                                                    redis=redis, engine_url=engine_url),
                                )
                                running_tasks[bot_id] = task
                                print(f"[BOTS] START  '{bot_row.name}' (strategy={bot_row.strategy})")
                                logger.info(json.dumps({
                                    "event": "BOT_TASK_STARTED",
                                    "bot_id": bot_id,
                                }))

                        elif action == "STOP" and bot_id and bot_id in running_tasks:
                            # Send STOP to the bot's own control stream
                            if redis:
                                from ib_trader.redis.streams import StreamWriter
                                writer = StreamWriter(redis, StreamNames.bot_control(bot_id), maxlen=100)
                                await writer.add({"action": "STOP"})
                            running_tasks[bot_id].cancel()
                            del running_tasks[bot_id]
                            print(f"[BOTS] STOP   bot_id={bot_id}")
                            logger.info(json.dumps({
                                "event": "BOT_TASK_STOPPED",
                                "bot_id": bot_id,
                            }))

                        elif action == "FORCE_BUY" and bot_id:
                            # Forward to the bot's control stream
                            if redis:
                                from ib_trader.redis.streams import StreamWriter
                                writer = StreamWriter(redis, StreamNames.bot_control(bot_id), maxlen=100)
                                await writer.add({"action": "FORCE_BUY"})
                            logger.info(json.dumps({
                                "event": "FORCE_BUY_FORWARDED",
                                "bot_id": bot_id,
                            }))

        except asyncio.CancelledError:
            # Shutdown: cancel all running bots
            for bot_id, task in running_tasks.items():
                task.cancel()
            await asyncio.gather(*running_tasks.values(), return_exceptions=True)
            raise
        except Exception:
            logger.exception(json.dumps({"event": "BOT_RUNNER_ERROR"}))
            await asyncio.sleep(1)
