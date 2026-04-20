"""Bot runner — manages bot lifecycle.

Lifecycle commands (start / stop / force-buy) arrive via the runner's
internal HTTP API (``ib_trader/bots/internal_api.py``) which holds the
authoritative bot-instance and task registries.

For each running bot we spawn a single asyncio task — the lifecycle
coordinator (``_run_bot_lifecycle``) — which itself runs two child
tasks in parallel:

  1. ``bot.run_event_loop()`` — multiplexes Redis streams (quotes,
     bars, order updates, position changes) into the strategy.
  2. ``_supervisory_loop()`` — fires every 10s for heartbeat refresh,
     entry-timeout enforcement, and stale-quote watchdog.

If either child exits (clean stop, crash, cancellation) the other is
cancelled too, and the coordinator returns. The runner's cleanup loop
detects coordinator-task completion and dispatches CRASH events for
unhandled exceptions.

Bot identity comes from the in-memory BotDefinition registry (loaded
from ``config/bots/*.yaml``). Runtime state (FSM, heartbeat, kill
switch) lives in Redis. SQLite is used only for audit writes
(bot_events).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import scoped_session

from ib_trader.data.models import BotEvent
from ib_trader.data.repositories.bot_repository import BotEventRepository
from ib_trader.bots.definition import BotDefinition
from ib_trader.bots.registry import get_strategy_class

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


_SUPERVISORY_INTERVAL_SECONDS = 10


async def _supervisory_loop(bot, defn: BotDefinition) -> None:
    """Periodic heartbeat / entry-timeout / stale-quote watchdog.

    Runs alongside ``bot.run_event_loop()`` in the lifecycle coordinator.
    Every iteration is wrapped so a transient failure (e.g. a Redis
    blip) doesn't take the whole loop down — but ``CancelledError``
    propagates so cooperative shutdown still works.
    """
    while True:
        await asyncio.sleep(_SUPERVISORY_INTERVAL_SECONDS)
        try:
            await bot.update_heartbeat()
            await bot.check_entry_timeout()
            await bot.check_stale_quote()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                '{"event": "SUPERVISORY_ERROR", "bot_id": "%s"}', defn.id,
            )


async def _run_bot_lifecycle(bot, defn: BotDefinition) -> None:
    """Run the bot's event loop and supervisory loop in parallel.

    Uses ``asyncio.wait(FIRST_COMPLETED)`` so a crash or cancellation in
    either child propagates: the surviving child is cancelled, then we
    re-raise the original exception (if any) so the outer cleanup loop
    sees it via ``task.exception()`` and dispatches a CRASH event.
    """
    main = asyncio.create_task(
        bot.run_event_loop(), name=f"bot-events-{defn.id}",
    )
    sup = asyncio.create_task(
        _supervisory_loop(bot, defn), name=f"bot-sup-{defn.id}",
    )
    children = [main, sup]
    try:
        done, _pending = await asyncio.wait(
            children, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                raise exc
    finally:
        for t in children:
            if not t.done():
                t.cancel()
        await asyncio.gather(*children, return_exceptions=True)


async def _create_and_start_bot(
    defn: BotDefinition, session_factory: scoped_session,
    redis=None, engine_url: str | None = None,
) -> tuple:
    """Create a bot instance, initialize it, and spawn its lifecycle task.

    Returns ``(bot_instance, asyncio.Task)``. The task wraps both the
    event loop and the supervisory loop so heartbeat, entry-timeout and
    stale-quote checks actually run.
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

    task = asyncio.create_task(
        _run_bot_lifecycle(bot, defn),
        name=f"bot-{defn.id}",
    )

    print(f"[BOTS] START  '{defn.name}' (strategy={defn.strategy})")
    logger.info(json.dumps({
        "event": "BOT_TASK_STARTED",
        "bot_id": defn.id,
    }))

    return bot, task


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
            for _bot_id, task in running_tasks.items():
                task.cancel()
            await asyncio.gather(*running_tasks.values(), return_exceptions=True)
            raise
        except Exception:
            logger.exception(json.dumps({"event": "BOT_RUNNER_ERROR"}))
            await asyncio.sleep(1)
