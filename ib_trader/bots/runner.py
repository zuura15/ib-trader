"""Bot runner — manages bot lifecycle via Redis streams.

The bot runner listens on the bot:control:* stream for START/STOP/FORCE_BUY
commands. No polling — all lifecycle changes are event-driven via XREAD BLOCK.

On startup, checks Redis for any bots marked RUNNING and restarts them.
Bot identity comes from the in-memory BotDefinition registry (loaded from
config/bots/*.yaml). Runtime state (status, heartbeat) lives in Redis.
SQLite is used ONLY for audit writes (bot_events).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from redis.exceptions import ConnectionError as _RedisConnectionError

from sqlalchemy.orm import scoped_session

from ib_trader.data.models import BotEvent
from ib_trader.data.repositories.bot_repository import BotEventRepository
from ib_trader.bots.base import BotBase
from ib_trader.bots.definition import BotDefinition
from ib_trader.bots.registry import get_strategy_class
from ib_trader.bots.state import BotStateStore, STATUS_RUNNING, STATUS_ERROR

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _create_and_start_bot(
    defn: BotDefinition, session_factory: scoped_session,
    redis=None, engine_url: str | None = None,
) -> tuple:
    """Create a bot instance, initialize it, and spawn its event loop task.

    Returns (bot_instance, asyncio.Task). Used by the internal HTTP API
    for the /start endpoint.
    """
    strategy_cls = get_strategy_class(defn.strategy)
    if strategy_cls is None:
        raise ValueError(f"Unknown strategy: {defn.strategy}")

    config = dict(defn.config)
    config["tick_interval_seconds"] = defn.tick_interval_seconds
    config["_redis"] = redis
    config["_engine_url"] = engine_url
    bot = strategy_cls(defn.id, config, session_factory)

    # Initialize the bot (strategy, middleware, aggregator, warmup)
    await bot.on_startup([])

    # Log start event
    events_repo = BotEventRepository(session_factory)
    events_repo.insert(BotEvent(
        bot_id=defn.id, event_type="STARTED",
        message=f"Bot started: {defn.name} ({defn.strategy})",
        recorded_at=_now_utc(),
    ))

    # Spawn the event loop as a task
    task = asyncio.create_task(
        bot.run_event_loop(),
        name=f"bot-{defn.id}",
    )

    print(f"[BOTS] START  '{defn.name}' (strategy={defn.strategy})")
    logger.info(json.dumps({
        "event": "BOT_TASK_STARTED",
        "bot_id": defn.id,
    }))

    return bot, task


async def _run_single_bot(defn: BotDefinition, session_factory: scoped_session,
                           recover: bool = False,
                           redis=None, engine_url: str | None = None) -> None:
    """Run a single bot until stopped via control stream or crash."""
    strategy_cls = get_strategy_class(defn.strategy)
    if strategy_cls is None:
        print(f"[BOTS] ERROR  Unknown strategy '{defn.strategy}' for bot '{defn.name}'")
        logger.error(json.dumps({
            "event": "UNKNOWN_STRATEGY",
            "bot_id": defn.id,
            "strategy": defn.strategy,
        }))
        state = BotStateStore(redis)
        await state.set_status(
            defn.id, STATUS_ERROR,
            error_message=f"Unknown strategy: {defn.strategy}",
        )
        return

    config = dict(defn.config)
    config["tick_interval_seconds"] = defn.tick_interval_seconds
    config["_redis"] = redis
    config["_engine_url"] = engine_url
    bot = strategy_cls(defn.id, config, session_factory)

    events_repo = BotEventRepository(session_factory)
    state = BotStateStore(redis)

    async def _nudge(channel: str) -> None:
        if redis is None:
            return
        from ib_trader.redis.streams import publish_activity
        await publish_activity(redis, channel)

    events_repo.insert(BotEvent(
        bot_id=defn.id, event_type="STARTED",
        message=f"Bot started: {defn.name} ({defn.strategy})",
        recorded_at=_now_utc(),
    ))
    await _nudge("bot_events")
    await _nudge("bots")

    # Always call on_startup — it initializes the strategy, context,
    # middleware pipeline, and bar aggregator. The recover flag only
    # controls whether to pass existing open positions for crash recovery.
    open_positions = []
    if recover:
        from ib_trader.data.repository import TradeRepository
        trades_repo = TradeRepository(session_factory)
        open_positions = trades_repo.get_open()
    await bot.on_startup(open_positions)

    try:
        await _stream_driven_loop(bot, defn, redis, state, events_repo)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[BOTS] ERROR  Bot '{defn.name}' failed: {e}")
        logger.exception(json.dumps({
            "event": "BOT_ERROR",
            "bot_id": defn.id,
            "error": str(e),
        }))
        events_repo.insert(BotEvent(
            bot_id=defn.id, event_type="ERROR",
            message=str(e),
            recorded_at=_now_utc(),
        ))
        await state.set_status(defn.id, STATUS_ERROR, error_message=str(e))
        await _nudge("bot_events")
        await _nudge("bots")
    finally:
        try:
            await bot.on_stop()
        except Exception:
            logger.exception(json.dumps({
                "event": "BOT_STOP_ERROR", "bot_id": defn.id,
            }))
        events_repo.insert(BotEvent(
            bot_id=defn.id, event_type="STOPPED",
            message=f"Bot stopped: {defn.name}",
            recorded_at=_now_utc(),
        ))
        await _nudge("bot_events")
        await _nudge("bots")


async def _stream_driven_loop(bot, defn: BotDefinition, redis,
                               state: BotStateStore, events_repo) -> None:
    """Run the bot as parallel asyncio tasks, all event-driven."""
    if not redis:
        raise RuntimeError("Redis required for event-driven bot runner")

    stop_event = asyncio.Event()

    event_task = asyncio.create_task(
        bot.run_event_loop(),
        name=f"bot-events-{defn.id}",
    )
    control_task = asyncio.create_task(
        _control_consumer(bot, defn, redis, stop_event),
        name=f"bot-control-{defn.id}",
    )
    supervisory_task = asyncio.create_task(
        _supervisory_loop(bot, defn, stop_event),
        name=f"bot-supervisor-{defn.id}",
    )

    tasks = [event_task, control_task, supervisory_task]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
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


async def _control_consumer(bot, defn: BotDefinition, redis,
                             stop_event: asyncio.Event) -> None:
    """Listen on the bot's control stream for STOP / FORCE_BUY events."""
    from ib_trader.redis.streams import StreamNames
    control_stream = StreamNames.bot_control(defn.id)
    control_last_id = "$"
    force_key = f"bot:{defn.id}:force_buy"

    while True:
        try:
            results = await redis.xread({control_stream: control_last_id}, block=5000)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, _RedisConnectionError):
            raise asyncio.CancelledError()
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
                            defn.id,
                        )
                        stop_event.set()
                        return
                    elif action == "FORCE_BUY":
                        logger.info(
                            '{"event": "FORCE_BUY_VIA_STREAM", "bot_id": "%s"}',
                            defn.id,
                        )
                        await bot.update_action("FORCE_BUY")
                        await bot.check_force_buy()

        try:
            if await redis.get(force_key):
                await redis.delete(force_key)
                logger.info(
                    '{"event": "FORCE_BUY_VIA_KEY", "bot_id": "%s"}',
                    defn.id,
                )
                await bot.update_action("FORCE_BUY")
                await bot.check_force_buy()
        except Exception:
            pass


async def _supervisory_loop(bot, defn: BotDefinition,
                             stop_event: asyncio.Event) -> None:
    """Periodic supervisory tasks — heartbeat, entry timeout, stale quote check."""
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10)
            return
        except asyncio.TimeoutError:
            pass

        try:
            await bot.update_heartbeat()
            await bot.check_entry_timeout()
            await bot.check_stale_quote()
        except Exception:
            logger.exception(
                '{"event": "SUPERVISORY_ERROR", "bot_id": "%s"}', defn.id,
            )


async def run_bot_runner(session_factory: scoped_session,
                         redis=None, engine_url: str | None = None,
                         running_tasks: dict | None = None,
                         bot_instances: dict | None = None) -> None:
    """Main bot runner — cleanup loop for crashed tasks.

    Lifecycle commands (start/stop/force-buy) are handled by the runner's
    internal HTTP API via direct method calls. This loop only:
    1. Restarts bots whose FSM was in a running state on startup
    2. Cleans up crashed tasks every 5s
    """
    from ib_trader.bots import registry_config
    from ib_trader.bots.fsm import FSM, BotEvent, BotState, EventType

    if running_tasks is None:
        running_tasks = {}
    if bot_instances is None:
        bot_instances = {}
    _RUNNING_FSM_STATES = {
        BotState.AWAITING_ENTRY_TRIGGER,
        BotState.ENTRY_ORDER_PLACED,
        BotState.AWAITING_EXIT_TRIGGER,
        BotState.EXIT_ORDER_PLACED,
    }

    logger.info(json.dumps({"event": "BOT_RUNNER_STARTED"}))

    # Startup: restart bots whose FSM is in a running state
    all_defs = registry_config.all_definitions()
    for defn in all_defs:
        fsm = FSM(defn.id, redis)
        cur = await fsm.current_state()
        if cur in _RUNNING_FSM_STATES:
            try:
                bot, task = await _create_and_start_bot(
                    defn, session_factory, redis=redis, engine_url=engine_url,
                )
                running_tasks[defn.id] = task
                bot_instances[defn.id] = bot
            except Exception:
                logger.exception(
                    '{"event": "BOT_RESTART_FAILED", "bot_id": "%s"}', defn.id,
                )

    # Main loop: just clean up crashed tasks. All lifecycle commands
    # flow through the runner's internal HTTP API via direct method calls.
    while True:
        try:
            await asyncio.sleep(5)

            for bot_id in list(running_tasks.keys()):
                task = running_tasks[bot_id]
                if task.done():
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        try:
                            await FSM(bot_id, redis).dispatch(BotEvent(
                                EventType.CRASH,
                                payload={"message": str(exc)},
                            ))
                        except Exception:
                            logger.exception('{"event": "FSM_CRASH_DISPATCH_FAILED", "bot_id": "%s"}', bot_id)
                        print(f"[BOTS] CRASH  bot_id={bot_id}: {exc}")
                    del running_tasks[bot_id]
                    bot_instances.pop(bot_id, None)

        except asyncio.CancelledError:
            for bot_id, task in running_tasks.items():
                task.cancel()
            await asyncio.gather(*running_tasks.values(), return_exceptions=True)
            raise
        except Exception:
            logger.exception(json.dumps({"event": "BOT_RUNNER_ERROR"}))
            await asyncio.sleep(1)
