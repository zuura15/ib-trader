"""Bot runner CLI entry point.

The bot runner is a separate process that manages bot lifecycle.
It has NO broker connection — bots submit commands via the
pending_commands table and read state from SQLite.

Usage:
    ib-bots                      # defaults
    ib-bots --db trader.db
"""
import asyncio
import logging
import os
from pathlib import Path

import click

from ib_trader.config.loader import load_settings, check_file_permissions
from ib_trader.data.repository import create_db_engine, create_session_factory, init_db
from ib_trader.data.repository import HeartbeatRepository
from ib_trader.logging_.logger import setup_logging

logger = logging.getLogger(__name__)


@click.command()
@click.option("--db", default="trader.db", help="SQLite database path")
@click.option("--settings", "settings_path", default="config/settings.yaml",
              help="Settings YAML path")
def main(db: str, settings_path: str):
    """IB Trader Bot Runner — manages bot lifecycle."""
    setup_logging()
    settings = load_settings(settings_path)

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

    # Import all bot strategies so they register themselves
    _import_strategies()

    # Bootstrap bots table from config/bots/*.yaml (YAML is authoritative).
    # Hard-fails on foreign SQLite rows so we don't run in a drifted state.
    from ib_trader.bots.bootstrap import bootstrap_bots_from_yaml, BootstrapError
    try:
        report = bootstrap_bots_from_yaml(session_factory)
        print(
            f"[BOTS] Bootstrap: +{len(report.added)} ~{len(report.updated)} "
            f"={len(report.unchanged)} -{len(report.removed)}"
        )
    except BootstrapError as exc:
        print(f"[BOTS] BOOTSTRAP REFUSED: {exc}")
        logger.error('{"event": "BOT_BOOTSTRAP_REFUSED", "error": "%s"}', exc)
        raise SystemExit(2) from exc

    asyncio.run(run(session_factory))


def _import_strategies():
    """Import all bot strategy modules to trigger register_strategy() calls."""
    try:
        import ib_trader.bots.examples.mean_revert  # noqa: F401
    except ImportError:
        pass
    try:
        import ib_trader.bots.runtime  # noqa: F401  — registers strategy_bot
    except ImportError:
        pass


async def run(session_factory) -> None:
    """Main bot runner coroutine."""
    pid = os.getpid()
    settings = load_settings("config/settings.yaml")

    heartbeats = HeartbeatRepository(session_factory)
    heartbeats.upsert("BOT_RUNNER", pid)
    logger.info('{"event": "BOT_RUNNER_STARTED", "pid": %d}', pid)
    print(f"[BOTS] Started (pid={pid}). No broker connection needed.")

    # Connect to Redis (required)
    redis_url = settings.get("redis_url", "redis://localhost:6379/0")
    from ib_trader.redis.client import get_redis
    redis = await get_redis(redis_url)
    print("[BOTS] Connected to Redis.")

    engine_url = f"http://127.0.0.1:{settings.get('engine_internal_port', 8081)}"

    # Start heartbeat loop
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(heartbeats, pid)
    )

    try:
        from ib_trader.bots.runner import run_bot_runner
        await run_bot_runner(session_factory, redis=redis, engine_url=engine_url)
    except KeyboardInterrupt:
        pass
    finally:
        heartbeat_task.cancel()
        heartbeats.delete("BOT_RUNNER")
        try:
            from ib_trader.redis.client import close_redis
            await close_redis()
        except Exception:
            pass
        logger.info('{"event": "BOT_RUNNER_STOPPED"}')


async def _heartbeat_loop(heartbeats, pid: int) -> None:
    """Write BOT_RUNNER heartbeat periodically."""
    while True:
        try:
            heartbeats.upsert("BOT_RUNNER", pid)
        except Exception:
            logger.exception('{"event": "HEARTBEAT_WRITE_FAILED"}')
        await asyncio.sleep(30)


if __name__ == "__main__":
    main()
