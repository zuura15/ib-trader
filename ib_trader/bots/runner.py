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


async def _panic_alert_on_startup(redis, defn, prior_state) -> None:
    """Publish a CATASTROPHIC alert when a bot's prior state implies
    a held position or in-flight order.

    The alert is written to the existing ``alerts:active`` Redis hash
    so the WebSocket alerts channel picks it up and the frontend's
    CatastrophicOverlay renders a blocking modal. ``pager=true`` so a
    future real-pager integration consumes the same feed.
    """
    if redis is None:
        logger.error(
            '{"event": "BOT_STARTUP_PANIC_NO_REDIS", "bot_id": "%s", '
            '"prior_state": "%s"}',
            defn.id, getattr(prior_state, "value", str(prior_state)),
        )
        return

    from ib_trader.redis.state import StateKeys, StateStore
    from ib_trader.redis.streams import publish_activity
    import uuid as _uuid

    # Best-effort: pull last-known position/order context from the bot
    # state doc so the operator has something to investigate with.
    try:
        state_doc = await StateStore(redis).get(f"bot:{defn.id}") or {}
    except Exception:
        logger.exception(
            '{"event": "BOT_STARTUP_PANIC_STATE_READ_FAILED", "bot_id": "%s"}',
            defn.id,
        )
        state_doc = {}

    symbol = state_doc.get("symbol") or defn.config.get("symbol") or defn.symbol
    qty = state_doc.get("qty")
    entry_price = state_doc.get("entry_price")
    ib_order_id = state_doc.get("ib_order_id")

    alert_id = str(_uuid.uuid4())
    alert_dict = {
        "id": alert_id,
        "severity": "CATASTROPHIC",
        "trigger": "BOT_ACTIVE_STATE_AT_STARTUP",
        "message": (
            f"Bot '{defn.name}' was in state {getattr(prior_state, 'value', prior_state)} "
            f"on restart. This implies a live IB position or an in-flight order. "
            f"Forcing bot to OFF. Check TWS for working orders and residual "
            f"position on {symbol}; resolve manually; re-enable the bot when clean."
        ),
        "created_at": _now_utc().isoformat(),
        "pager": True,
        "bot_id": defn.id,
        "bot_name": defn.name,
        "symbol": symbol,
        "prior_state": getattr(prior_state, "value", str(prior_state)),
        "qty": qty,
        "entry_price": entry_price,
        "ib_order_id": ib_order_id,
    }
    try:
        await StateKeys.publish_alert(redis, alert_id, alert_dict)
        await publish_activity(redis, "alerts")
    except Exception:
        logger.exception(
            '{"event": "BOT_STARTUP_PANIC_ALERT_PUBLISH_FAILED", "bot_id": "%s"}',
            defn.id,
        )
    logger.error(
        '{"event": "BOT_ACTIVE_STATE_AT_STARTUP", "bot_id": "%s", '
        '"symbol": "%s", "prior_state": "%s", "qty": "%s", "entry_price": "%s", '
        '"ib_order_id": "%s"}',
        defn.id, symbol, getattr(prior_state, "value", prior_state),
        qty, entry_price, ib_order_id,
    )


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
    # Pull global tunables that apply to every bot — avoids threading a
    # settings dict through every call site for a handful of keys.
    try:
        from ib_trader.config.loader import load_settings
        _settings = load_settings("config/settings.yaml")
        if "market_data_heartbeat_stale_halt_seconds" in _settings:
            config["market_data_heartbeat_stale_halt_seconds"] = \
                _settings["market_data_heartbeat_stale_halt_seconds"]
    except Exception:
        logger.debug("settings_load_failed", exc_info=True)
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
    from ib_trader.bots.lifecycle import BotState, force_off_state

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

    # Startup policy (Apr 19 incident fix):
    # ------------------------------------
    # Every bot is forced to OFF at app startup regardless of prior
    # FSM state. The user re-enables bots explicitly via the UI once
    # they've confirmed the external world (TWS positions + open orders)
    # is consistent. Prior states that imply a held position or an
    # in-flight order raise a CATASTROPHIC "pager" alert so the operator
    # doesn't miss it.
    #
    # Panic triggers (alert + force OFF):
    #   ENTRY_ORDER_PLACED, AWAITING_EXIT_TRIGGER, EXIT_ORDER_PLACED, ERRORED
    # Silent force OFF (no alert):
    #   AWAITING_ENTRY_TRIGGER — no real-money implication
    #   OFF — already there
    all_defs = registry_config.all_definitions()
    _PANIC_STATES = {
        BotState.ENTRY_ORDER_PLACED,
        BotState.AWAITING_EXIT_TRIGGER,
        BotState.EXIT_ORDER_PLACED,
        BotState.ERRORED,
    }
    for defn in all_defs:
        # Read the bot's current lifecycle state from Redis without an
        # instance. No FSM dispatch — the panic path is a pure Redis
        # write via ``force_off_state``.
        cur = BotState.OFF
        if redis is not None:
            try:
                from ib_trader.redis.state import StateStore
                doc = await StateStore(redis).get(f"bot:{defn.id}") or {}
                try:
                    cur = BotState(doc.get("state", BotState.OFF.value))
                except ValueError:
                    cur = BotState.OFF
            except Exception:
                logger.exception(
                    '{"event": "BOT_STARTUP_STATE_LOAD_FAILED", "bot_id": "%s"}',
                    defn.id,
                )
                continue

        if cur in _PANIC_STATES:
            await _panic_alert_on_startup(redis, defn, cur)
            try:
                await force_off_state(
                    defn.id, redis, reason="startup_forced_off_with_panic",
                )
            except Exception:
                logger.exception(
                    '{"event": "BOT_FORCE_OFF_FAILED", "bot_id": "%s"}',
                    defn.id,
                )
            logger.warning(
                '{"event": "BOT_STARTUP_FORCED_OFF_WITH_PANIC", '
                '"bot_id": "%s", "prior_state": "%s"}',
                defn.id, cur.value,
            )
        elif cur != BotState.OFF:
            # AWAITING_ENTRY_TRIGGER — silent force OFF, no alert.
            try:
                await force_off_state(
                    defn.id, redis, reason="startup_forced_off",
                )
            except Exception:
                logger.exception(
                    '{"event": "BOT_FORCE_OFF_FAILED", "bot_id": "%s"}',
                    defn.id,
                )
            logger.info(
                '{"event": "BOT_STARTUP_FORCED_OFF", "bot_id": "%s", '
                '"prior_state": "%s"}', defn.id, cur.value,
            )
        # OFF: nothing to do.

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
                        # Bot task crashed mid-session. Invoke the bot's
                        # on_crash method if the instance is still around;
                        # fall back to a direct Redis write if not.
                        bot = bot_instances.get(bot_id)
                        if bot is not None:
                            try:
                                await bot.on_crash(message=str(exc))
                            except Exception:
                                logger.exception(
                                    '{"event": "BOT_CRASH_HANDLER_FAILED", '
                                    '"bot_id": "%s"}', bot_id,
                                )
                        else:
                            # No instance — write ERRORED directly.
                            try:
                                from ib_trader.redis.state import StateStore
                                from ib_trader.bots.lifecycle import (
                                    BotState, bot_doc_key, now_iso,
                                )
                                store = StateStore(redis)
                                doc = await store.get(bot_doc_key(bot_id)) or {}
                                doc.update({
                                    "state": BotState.ERRORED.value,
                                    "error_reason": "task_crashed",
                                    "error_message": str(exc),
                                    "updated_at": now_iso(),
                                })
                                await store.set(bot_doc_key(bot_id), doc)
                            except Exception:
                                logger.exception(
                                    '{"event": "BOT_CRASH_STATE_WRITE_FAILED", '
                                    '"bot_id": "%s"}', bot_id,
                                )
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
