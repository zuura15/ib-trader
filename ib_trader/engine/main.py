"""Engine service CLI entry point.

The engine service is the sole process with broker connections. It polls
pending_commands from SQLite, executes them via the trading engine, and
writes results back. All other processes (REPL, API, bots) submit commands
by inserting rows into the pending_commands table.

Usage:
    ib-engine                # defaults, IB broker
    ib-engine --paper        # paper trading
    ib-engine --db trader.db
"""
import asyncio
import logging
import os
from pathlib import Path

# Module-level event signaling that a command completed and positions should refresh.
position_refresh_event = asyncio.Event()

import click

from ib_trader.config.loader import load_env, load_settings, load_symbols, check_file_permissions
from ib_trader.config.context import AppContext
from ib_trader.data.repository import (
    TradeRepository, RepriceEventRepository,
    ContractRepository, HeartbeatRepository, AlertRepository,
    create_db_engine, create_session_factory, init_db,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
from ib_trader.data.repositories.template_repository import OrderTemplateRepository
from ib_trader.engine.tracker import OrderTracker
from ib_trader.logging_.logger import setup_logging

logger = logging.getLogger(__name__)


@click.command()
@click.option("--db", default="trader.db", help="SQLite database path")
@click.option("--env", default=".env", help="Environment file path")
@click.option("--settings", "settings_path", default="config/settings.yaml",
              help="Settings YAML path")
@click.option("--symbols", "symbols_path", default="config/symbols.yaml",
              help="Symbols whitelist path")
@click.option("--paper", is_flag=True, default=False, help="Use paper trading account")
def main(db: str, env: str, settings_path: str, symbols_path: str, paper: bool):
    """IB Trader Engine Service — central command execution loop."""
    setup_logging()

    # Load configuration (same pattern as REPL and daemon)
    env_vars = load_env(env)
    settings = load_settings(settings_path)
    symbols = load_symbols(symbols_path)

    # Override with env values
    settings["ib_host"] = env_vars.get("IB_HOST", settings.get("ib_host", "127.0.0.1"))
    if paper:
        settings["ib_port"] = int(env_vars.get("IB_PORT_PAPER", 4002))
        settings["ib_market_data_type"] = int(env_vars.get("IB_MARKET_DATA_TYPE_PAPER", 3))
        account_id = env_vars.get("IB_ACCOUNT_ID_PAPER") or env_vars["IB_ACCOUNT_ID"]
    else:
        settings["ib_port"] = int(env_vars.get("IB_PORT", 4001))
        settings["ib_market_data_type"] = int(env_vars.get("IB_MARKET_DATA_TYPE", 1))
        account_id = env_vars["IB_ACCOUNT_ID"]
    settings["ib_client_id"] = int(env_vars.get("IB_CLIENT_ID", 1))

    # Check DB permissions
    if Path(db).exists():
        try:
            check_file_permissions(db, 0o600, "SQLite database")
        except Exception as e:
            logger.warning('{"event": "DB_PERMISSION_WARNING", "error": "%s"}', str(e))

    # Create engine and session factory
    db_url = f"sqlite:///{db}"
    engine = create_db_engine(db_url)
    init_db(engine)
    session_factory = create_session_factory(engine)

    # Create IB client
    from ib_trader.ib.insync_client import InsyncClient
    ib_client = InsyncClient(
        host=settings["ib_host"],
        port=settings["ib_port"],
        client_id=settings["ib_client_id"],
        account_id=account_id,
        min_call_interval_ms=settings["ib_min_call_interval_ms"],
        market_data_type=settings["ib_market_data_type"],
    )

    # Assemble AppContext
    ctx = AppContext(
        ib=ib_client,
        trades=TradeRepository(session_factory),
        reprice_events=RepriceEventRepository(session_factory),
        contracts=ContractRepository(session_factory),
        heartbeats=HeartbeatRepository(session_factory),
        alerts=AlertRepository(session_factory),
        tracker=OrderTracker(),
        settings=settings,
        account_id=account_id,
        transactions=TransactionRepository(session_factory),
        pending_commands=PendingCommandRepository(session_factory),
        bots=BotRepository(session_factory),
        bot_events=BotEventRepository(session_factory),
        templates=OrderTemplateRepository(session_factory),
    )

    asyncio.run(run_engine(ctx, symbols))


async def run_engine(ctx: AppContext, symbols: list[str]) -> None:
    """Main engine service coroutine."""
    pid = os.getpid()
    retry_interval = ctx.settings.get("ib_connect_retry_seconds", 10)

    # Write heartbeat
    ctx.heartbeats.upsert("ENGINE", pid)
    logger.info('{"event": "ENGINE_STARTED", "pid": %d}', pid)
    print(f"[ENGINE] Started (pid={pid}). Connecting to IB Gateway...")

    # Connect to IB with retry loop
    await _connect_with_retry(ctx, retry_interval)

    print("[ENGINE] Connected to IB Gateway.")

    # Warm contract cache
    for symbol in symbols:
        try:
            await ctx.ib.qualify_contract(symbol)
        except Exception:
            logger.warning('{"event": "CONTRACT_WARM_FAILED", "symbol": "%s"}', symbol)

    print(f"[ENGINE] Warmed {len(symbols)} contracts. Processing commands...")

    # Start heartbeat + position cache loops
    heartbeat_task = asyncio.create_task(_heartbeat_loop(ctx, pid))
    position_task = asyncio.create_task(_position_cache_loop(ctx))

    try:
        from ib_trader.engine.service import engine_loop
        max_concurrent = ctx.settings.get("engine_max_concurrent", 5)
        poll_interval = ctx.settings.get("engine_poll_interval", 0.1)
        await engine_loop(ctx, max_concurrent=max_concurrent,
                          poll_interval=poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        heartbeat_task.cancel()
        position_task.cancel()
        ctx.heartbeats.delete("ENGINE")
        await ctx.ib.disconnect()
        print("[ENGINE] Stopped.")
        logger.info('{"event": "ENGINE_STOPPED"}')


async def _connect_with_retry(ctx: AppContext, retry_interval: int = 10) -> None:
    """Keep trying to connect to IB Gateway until successful.

    Prints a clear message every retry so the operator knows what's happening.
    """
    attempt = 0
    host = ctx.settings.get("ib_host", "127.0.0.1")
    port = ctx.settings.get("ib_port", 4001)

    while True:
        attempt += 1
        try:
            await ctx.ib.connect()
            logger.info('{"event": "IB_CONNECTED", "attempt": %d}', attempt)
            return
        except Exception as e:
            msg = (
                f"[ENGINE] IB Gateway not reachable at {host}:{port} "
                f"(attempt {attempt}). Retrying in {retry_interval}s... "
                f"({e})"
            )
            print(msg)
            logger.warning(
                '{"event": "IB_CONNECT_RETRY", "attempt": %d, "host": "%s", '
                '"port": %d, "error": "%s"}',
                attempt, host, port, str(e),
            )
            await asyncio.sleep(retry_interval)


async def _heartbeat_loop(ctx: AppContext, pid: int) -> None:
    """Write ENGINE heartbeat to SQLite periodically."""
    interval = ctx.settings.get("heartbeat_interval_seconds", 30)
    while True:
        try:
            ctx.heartbeats.upsert("ENGINE", pid)
        except Exception:
            logger.exception('{"event": "HEARTBEAT_WRITE_FAILED"}')
        await asyncio.sleep(interval)


async def _position_cache_loop(ctx: AppContext) -> None:
    """Periodically fetch positions from IB, read live streaming prices, and
    write to run/positions.json.

    Market data subscriptions are kept alive persistently — we subscribe once
    per contract and just read the latest ticker values each cycle.  This
    avoids the stale-ticker bug where reqMktData + cancelMktData reuses a
    recycled Ticker object with old values.

    The API server reads run/positions.json directly (no SQLite).
    """
    import json as _json
    from decimal import Decimal

    positions_path = Path("run/positions.json")
    positions_path.parent.mkdir(parents=True, exist_ok=True)

    class _Enc(_json.JSONEncoder):
        def default(self, o):
            if isinstance(o, Decimal):
                return str(o)
            return super().default(o)

    # Get the raw ib_async IB object for positions() and reqMktData()
    ib_obj = None
    if hasattr(ctx.ib, '_insync') and hasattr(ctx.ib._insync, '_ib'):
        ib_obj = ctx.ib._insync._ib
    elif hasattr(ctx.ib, '_ib'):
        ib_obj = ctx.ib._ib

    if ib_obj is None:
        logger.warning('{"event": "POSITION_CACHE_NO_IB_OBJ"}')
        return

    # Persistent streaming subscriptions: con_id → Ticker
    streaming_tickers: dict[int, object] = {}

    # Initial delay to let connection stabilize
    await asyncio.sleep(5)

    while True:
        try:
            # Refresh positions from IB
            try:
                await ib_obj.reqPositionsAsync()
            except Exception:
                logger.debug('{"event": "REQ_POSITIONS_FAILED"}')
            raw_positions = ib_obj.positions()

            # Ensure we have streaming subscriptions for all position contracts
            current_con_ids = set()
            for p in raw_positions:
                con_id = p.contract.conId
                current_con_ids.add(con_id)
                if con_id not in streaming_tickers:
                    try:
                        from ib_async import Contract
                        # Get qualified contract from the cache or build one
                        contract = (
                            ctx.ib._contract_cache.get(con_id)
                            if hasattr(ctx.ib, '_contract_cache')
                            else None
                        ) or Contract(
                            conId=con_id,
                            symbol=p.contract.symbol,
                            secType=p.contract.secType,
                            exchange=p.contract.exchange or "SMART",
                            currency=p.contract.currency or "USD",
                        )
                        ticker = ib_obj.reqMktData(contract, "", False, False)
                        streaming_tickers[con_id] = ticker
                        logger.info(
                            '{"event": "STREAMING_SUB_ADDED", "symbol": "%s", "con_id": %d}',
                            p.contract.symbol, con_id,
                        )
                    except Exception:
                        logger.debug(
                            '{"event": "STREAMING_SUB_FAILED", "con_id": %d}',
                            con_id,
                        )

            # Cancel subscriptions for positions we no longer hold
            for gone_id in list(streaming_tickers.keys() - current_con_ids):
                try:
                    ib_obj.cancelMktData(streaming_tickers[gone_id].contract)
                except Exception:
                    pass
                del streaming_tickers[gone_id]

            # Build output — read latest values from streaming tickers (instant)
            positions = []
            for idx, p in enumerate(raw_positions):
                sym = p.contract.symbol
                sec = p.contract.secType
                con_id = p.contract.conId

                mkt_price = None
                ticker = streaming_tickers.get(con_id)
                if ticker is not None:
                    bid = getattr(ticker, 'bid', None)
                    ask = getattr(ticker, 'ask', None)
                    last = getattr(ticker, 'last', None)

                    # Use bid/ask midpoint (updates continuously).
                    # Fall back to last trade price.
                    if (bid and bid == bid and bid > 0
                            and ask and ask == ask and ask > 0):
                        mkt_price = (bid + ask) / 2
                    elif last and last == last and last > 0:
                        mkt_price = last

                positions.append({
                    "id": f"{sym}_{sec}_{idx}",
                    "account_id": p.account,
                    "symbol": sym,
                    "sec_type": sec,
                    "quantity": str(p.position),
                    "avg_cost": str(p.avgCost),
                    "market_price": f"{mkt_price:.4f}" if mkt_price is not None else None,
                    "broker": "ib",
                })

            # Atomic write
            tmp_path = positions_path.with_suffix(".tmp")
            tmp_path.write_text(
                _json.dumps(positions, cls=_Enc),
                encoding="utf-8",
            )
            tmp_path.rename(positions_path)

            logger.debug(
                '{"event": "POSITION_FILE_WRITTEN", "count": %d}',
                len(positions),
            )

        except Exception:
            logger.exception('{"event": "POSITION_CACHE_ERROR"}')

        # Wait up to 2s, or wake immediately if a command completed.
        # With streaming data, reading tickers is free — we can refresh fast.
        try:
            await asyncio.wait_for(position_refresh_event.wait(), timeout=2)
            position_refresh_event.clear()
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    main()
