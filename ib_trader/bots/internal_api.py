"""Bot runner internal HTTP API — direct method calls to bot instances.

The public API server proxies lifecycle operations here. The runner
calls bot methods directly — no Redis keys, no control streams, no
polling. Lifecycle state transitions happen inside the bot via the
``on_*`` methods (see ``runtime.py``; historical context in
``docs/decisions/016-collapse-fsm-into-bot.md``).
"""
import asyncio
import logging

from fastapi import FastAPI, HTTPException

from ib_trader.bots.lifecycle import (
    BotState, force_off_state, is_clean_for_start,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="IB Trader Bot Runner Internal API")

_runner_state: dict | None = None

# Sentinel used in the bot_instances dict between the moment we accept
# a START request and the moment we've successfully created the bot +
# its task. Any concurrent START for the same bot_id sees the sentinel
# and returns 409 instead of kicking off a parallel instantiation.
_RESERVED = object()


def set_runner_state(state: dict) -> None:
    global _runner_state
    _runner_state = state


def _get_state() -> dict:
    if _runner_state is None:
        raise HTTPException(status_code=503, detail="Runner not initialized")
    return _runner_state


@app.get("/health")
async def health():
    """Liveness probe for the bot runner process.

    Lightweight — reports PID and active-bot count only. No Redis
    call, no bot state traversal. Polled every 60s by the external
    pager (see `ops/health_check.sh`, GH #47).
    """
    import os as _os
    active = 0
    if _runner_state is not None:
        running = _runner_state.get("running_tasks") or {}
        active = sum(1 for t in running.values() if t and not t.done())
    return {"status": "ok", "pid": _os.getpid(), "bots_active": active}


@app.post("/bots/{bot_id}/start")
async def start_bot(bot_id: str):
    state = _get_state()
    running_tasks = state["running_tasks"]
    bot_instances = state["bot_instances"]
    redis = state["redis"]
    registry = state["registry"]
    session_factory = state["session_factory"]
    engine_url = state["engine_url"]

    # Reservation pattern: both ``in`` check and assignment are
    # synchronous (no ``await``) so they run atomically with respect
    # to every other coroutine in this process. Prevents the
    # check-then-await-then-insert race where two concurrent START
    # requests both pass the ``in`` check during
    # ``_create_and_start_bot``'s warmup and end up with two tasks.
    if bot_instances.get(bot_id) is not None:
        cur = BotState.OFF
        if redis is not None:
            try:
                from ib_trader.redis.state import StateStore
                doc = await StateStore(redis).get(f"bot:{bot_id}") or {}
                cur = BotState(doc.get("state", BotState.OFF.value))
            except Exception:
                pass
        return {"bot_id": bot_id, "state": cur.value, "message": "already running"}

    defn = registry.get(bot_id)
    if defn is None:
        raise HTTPException(status_code=404, detail="Bot not found in registry")

    # Self-check: require a clean (OFF + zeroed) doc before starting.
    # A bot in ERRORED or with lingering position fields must be reset
    # first (``/bots/<id>/reset``) — errors don't auto-clear on START.
    if redis is not None:
        from ib_trader.redis.state import StateStore
        doc = await StateStore(redis).get(f"bot:{bot_id}")
        ok, reason = is_clean_for_start(doc)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Bot doc not clean for start: {reason}. "
                    f"Call /bots/{bot_id}/reset first."
                ),
            )

    # Reserve the slot before the await so concurrent callers see it.
    bot_instances[bot_id] = _RESERVED
    try:
        from ib_trader.bots.runner import _create_and_start_bot
        bot, task = await _create_and_start_bot(
            defn, session_factory, redis=redis, engine_url=engine_url,
        )
        running_tasks[bot_id] = task
        bot_instances[bot_id] = bot

        # State transition AFTER task is created (authoritative) —
        # the bot's on_start flips OFF → AWAITING_ENTRY_TRIGGER.
        await bot.on_start(symbol=defn.config.get("symbol"))
    except Exception:
        # Pipeline failed — release the reservation so a retry can
        # proceed.
        if bot_instances.get(bot_id) is _RESERVED:
            bot_instances.pop(bot_id, None)
        raise

    logger.info('{"event": "BOT_STARTED_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": BotState.AWAITING_ENTRY_TRIGGER.value}


@app.post("/bots/{bot_id}/stop")
async def stop_bot(bot_id: str):
    state = _get_state()
    running_tasks = state["running_tasks"]
    bot_instances = state["bot_instances"]
    redis = state["redis"]

    bot = bot_instances.pop(bot_id, None)
    if bot is None or bot is _RESERVED:
        # No instance — best we can do is ensure the doc reads OFF.
        if redis is not None:
            try:
                from ib_trader.redis.state import StateStore
                doc = await StateStore(redis).get(f"bot:{bot_id}") or {}
                cur = BotState(doc.get("state", BotState.OFF.value))
                if cur != BotState.OFF:
                    await force_off_state(bot_id, redis, reason="stop_no_instance")
            except Exception:
                logger.exception(
                    '{"event": "BOT_STOP_DOC_READ_FAILED", "bot_id": "%s"}',
                    bot_id,
                )
        return {"bot_id": bot_id, "state": "OFF", "message": "no instance"}

    # Bot handles its own cancel-order side effect and state transition.
    # Must run while the task is still alive — the executor reaches the
    # engine via httpx.
    try:
        await bot.on_stop()
    except Exception:
        logger.exception(
            '{"event": "BOT_STOP_FAILED", "bot_id": "%s"}', bot_id,
        )
    if hasattr(bot, 'request_stop'):
        bot.request_stop()
    task = running_tasks.pop(bot_id, None)
    if task:
        task.cancel()
    # Post-cancel teardown (strategy.on_stop + unsubscribe bars).
    try:
        await bot.on_teardown()
    except Exception:
        logger.exception(
            '{"event": "BOT_TEARDOWN_FAILED", "bot_id": "%s"}', bot_id,
        )

    logger.info('{"event": "BOT_STOPPED_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": "OFF"}


@app.post("/bots/{bot_id}/force-stop")
async def force_stop_bot(bot_id: str):
    state = _get_state()
    running_tasks = state["running_tasks"]
    bot_instances = state["bot_instances"]
    redis = state["redis"]

    bot = bot_instances.pop(bot_id, None)
    if bot is not None and bot is not _RESERVED:
        try:
            await bot.on_force_stop(message="Operator force-stop via HTTP")
        except Exception:
            logger.exception(
                '{"event": "BOT_FORCE_STOP_FAILED", "bot_id": "%s"}', bot_id,
            )
        if hasattr(bot, 'request_stop'):
            bot.request_stop()
        task = running_tasks.pop(bot_id, None)
        if task:
            task.cancel()
    else:
        # No instance; write the ERRORED marker directly.
        if redis is not None:
            try:
                from ib_trader.redis.state import StateStore
                from ib_trader.bots.lifecycle import bot_doc_key, now_iso
                store = StateStore(redis)
                doc = await store.get(bot_doc_key(bot_id)) or {}
                doc.update({
                    "state": BotState.ERRORED.value,
                    "error_reason": "force_stop",
                    "error_message": "Operator force-stop via HTTP (no instance)",
                    "updated_at": now_iso(),
                })
                await store.set(bot_doc_key(bot_id), doc)
            except Exception:
                logger.exception(
                    '{"event": "BOT_FORCE_STOP_WRITE_FAILED", "bot_id": "%s"}',
                    bot_id,
                )

    logger.info('{"event": "BOT_FORCE_STOPPED_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": "ERRORED", "error_reason": "force_stop"}


@app.post("/bots/{bot_id}/reset")
async def reset_bot(bot_id: str):
    """Operator-driven reset — writes the bot's doc back to a clean
    OFF state. Required before re-STARTing a bot that's in ERRORED
    (task crash, force-stop, exit-retries exhausted) or has lingering
    position fields from an interrupted trade. Does NOT touch the
    running instance — if the bot is currently running this endpoint
    will refuse (use /stop first).
    """
    state = _get_state()
    bot_instances = state["bot_instances"]
    redis = state["redis"]

    if bot_instances.get(bot_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Bot is running — call /bots/{bot_id}/stop first.",
        )

    if redis is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    doc = await force_off_state(bot_id, redis, reason="operator_reset")
    logger.info('{"event": "BOT_RESET_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": doc.get("state"), "message": "reset"}


@app.post("/bots/{bot_id}/force-buy")
async def force_buy(bot_id: str):
    state = _get_state()
    bot_instances = state["bot_instances"]

    bot = bot_instances.get(bot_id)
    if bot is None or bot is _RESERVED:
        raise HTTPException(status_code=409, detail="Bot is not running")

    cur = await bot.current_state()
    if cur != BotState.AWAITING_ENTRY_TRIGGER:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot force-buy in state {cur.value}",
        )

    # No FSM pre-dispatch — on_place_entry_order is invoked inside
    # _run_pipeline (via force_buy → _execute_force_buy → pipeline)
    # BEFORE the engine HTTP call, which transitions state to
    # ENTRY_ORDER_PLACED synchronously and gates subsequent stream
    # events.
    try:
        result = await bot.force_buy()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info('{"event": "BOT_FORCE_BUY_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": "ENTRY_ORDER_PLACED", **result}


@app.post("/bots/{bot_id}/force-sell")
async def force_sell(bot_id: str):
    state = _get_state()
    bot_instances = state["bot_instances"]

    bot = bot_instances.get(bot_id)
    if bot is None or bot is _RESERVED:
        raise HTTPException(status_code=409, detail="Bot is not running")

    cur = await bot.current_state()
    if cur != BotState.AWAITING_EXIT_TRIGGER:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot force-sell in state {cur.value}",
        )

    try:
        result = await bot.force_sell()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info('{"event": "BOT_FORCE_SELL_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": "EXIT_ORDER_PLACED", **result}


async def start_bot_runner_api(runner_state: dict, port: int = 8082) -> asyncio.Task:
    """Start the bot runner's internal API as a background task."""
    import uvicorn

    set_runner_state(runner_state)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    logger.info('{"event": "BOT_RUNNER_API_STARTED", "port": %d}', port)
    return task
