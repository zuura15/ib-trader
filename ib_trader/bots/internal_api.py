"""Bot runner internal HTTP API — direct method calls to bot instances.

The public API server proxies lifecycle operations here. The runner
calls bot methods directly — no Redis keys, no control streams, no
polling. FSM transitions happen here before/after the method call.
"""
import asyncio
import logging

from fastapi import FastAPI, HTTPException

from ib_trader.bots.fsm import FSM, BotEvent, BotState, EventType

logger = logging.getLogger(__name__)

app = FastAPI(title="IB Trader Bot Runner Internal API")

_runner_state: dict | None = None


def set_runner_state(state: dict) -> None:
    global _runner_state
    _runner_state = state


def _get_state() -> dict:
    if _runner_state is None:
        raise HTTPException(status_code=503, detail="Runner not initialized")
    return _runner_state


@app.post("/bots/{bot_id}/start")
async def start_bot(bot_id: str):
    state = _get_state()
    running_tasks = state["running_tasks"]
    bot_instances = state["bot_instances"]
    redis = state["redis"]
    registry = state["registry"]
    session_factory = state["session_factory"]
    engine_url = state["engine_url"]

    if bot_id in bot_instances:
        fsm = FSM(bot_id, redis)
        cur = await fsm.current_state()
        return {"bot_id": bot_id, "state": cur.value, "message": "already running"}

    defn = registry.get(bot_id)
    if defn is None:
        raise HTTPException(status_code=404, detail="Bot not found in registry")

    # Create and initialize the bot instance
    from ib_trader.bots.runner import _create_and_start_bot
    bot, task = await _create_and_start_bot(
        defn, session_factory, redis=redis, engine_url=engine_url,
    )
    running_tasks[bot_id] = task
    bot_instances[bot_id] = bot

    # FSM transition AFTER task is created (authoritative)
    fsm = FSM(bot_id, redis)
    await fsm.dispatch(BotEvent(EventType.START))

    logger.info('{"event": "BOT_STARTED_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": BotState.AWAITING_ENTRY_TRIGGER.value}


@app.post("/bots/{bot_id}/stop")
async def stop_bot(bot_id: str):
    state = _get_state()
    running_tasks = state["running_tasks"]
    bot_instances = state["bot_instances"]
    redis = state["redis"]

    fsm = FSM(bot_id, redis)
    cur = await fsm.current_state()
    if cur == BotState.OFF:
        return {"bot_id": bot_id, "state": "OFF", "message": "already off"}

    # Dispatch STOP first so the FSM emits the cancel_order side effect
    # for any in-flight order while the bot's task is still alive (the
    # executor reaches out via httpx → engine and needs the event loop
    # running). Then signal stop + cancel the task.
    bot = bot_instances.pop(bot_id, None)
    result = await fsm.dispatch(BotEvent(EventType.STOP))
    if bot is not None and result is not None:
        await bot._execute_side_effects(result)
    if bot and hasattr(bot, 'request_stop'):
        bot.request_stop()
    task = running_tasks.pop(bot_id, None)
    if task:
        task.cancel()

    logger.info('{"event": "BOT_STOPPED_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": "OFF"}


@app.post("/bots/{bot_id}/force-stop")
async def force_stop_bot(bot_id: str):
    state = _get_state()
    running_tasks = state["running_tasks"]
    bot_instances = state["bot_instances"]
    redis = state["redis"]

    bot = bot_instances.pop(bot_id, None)
    fsm = FSM(bot_id, redis)
    # FORCE_STOP itself emits no side effects today, but pass the result
    # through the executor for symmetry / forward-compat. The cancel
    # of any in-flight order on operator-initiated force stop should be
    # added to ``_h_force_stop`` if/when desired.
    result = await fsm.dispatch(BotEvent(
        EventType.FORCE_STOP,
        payload={"message": "Operator force-stop via HTTP"},
    ))
    if bot is not None and result is not None:
        await bot._execute_side_effects(result)
    if bot and hasattr(bot, 'request_stop'):
        bot.request_stop()
    task = running_tasks.pop(bot_id, None)
    if task:
        task.cancel()

    logger.info('{"event": "BOT_FORCE_STOPPED_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": "ERRORED", "error_reason": "force_stop"}


@app.post("/bots/{bot_id}/force-buy")
async def force_buy(bot_id: str):
    state = _get_state()
    bot_instances = state["bot_instances"]
    redis = state["redis"]

    bot = bot_instances.get(bot_id)
    if bot is None:
        raise HTTPException(status_code=409, detail="Bot is not running")

    fsm = FSM(bot_id, redis)
    cur = await fsm.current_state()
    if cur != BotState.AWAITING_ENTRY_TRIGGER:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot force-buy in state {cur.value}",
        )

    # FSM transition FIRST — before order placement.
    # This ensures the FSM is in ENTRY_ORDER_PLACED before the fill
    # arrives on the stream, eliminating the race condition.
    defn = state["registry"].get(bot_id)
    symbol = defn.config.get("symbol", "") if defn else ""
    await fsm.dispatch(BotEvent(EventType.PLACE_ENTRY_ORDER, payload={
        "symbol": symbol,
        "qty": "0",  # actual qty computed by the bot
        "origin": "manual_override",
    }))

    # Direct method call — bot places the order via engine HTTP
    try:
        result = await bot.force_buy()
    except Exception as e:
        # Revert FSM on failure
        await fsm.dispatch(BotEvent(EventType.ENTRY_CANCELLED, payload={
            "reason": str(e),
        }))
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info('{"event": "BOT_FORCE_BUY_VIA_HTTP", "bot_id": "%s"}', bot_id)
    return {"bot_id": bot_id, "state": "ENTRY_ORDER_PLACED", **result}


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
