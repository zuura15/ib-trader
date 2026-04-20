"""API server CLI entry point.

The API server is a thin read layer + command submitter. It does NOT
hold any broker connections — all order execution goes through the
engine service via the pending_commands SQLite table.

Usage:
    ib-api                          # defaults
    ib-api --port 8080 --db trader.db
"""
import logging
import os
from pathlib import Path

import click
import uvicorn

from ib_trader.config.loader import load_env, load_settings, check_file_permissions
from ib_trader.data.repository import create_db_engine, create_session_factory, init_db
from ib_trader.data.repository import HeartbeatRepository
from ib_trader.logging_.logger import setup_logging

logger = logging.getLogger(__name__)


@click.command()
@click.option("--db", default="trader.db", help="SQLite database path")
@click.option("--env", default=".env", help="Environment file path")
@click.option("--settings", "settings_path", default="config/settings.yaml",
              help="Settings YAML path")
@click.option("--host", default="0.0.0.0", help="API server bind host")  # noqa: S104 — intentional LAN bind (see README)
@click.option("--port", default=8000, type=int, help="API server port")
def main(db: str, env: str, settings_path: str, host: str, port: int):
    """IB Trader API Server — REST API for the trading platform."""
    setup_logging()

    # Load configuration
    load_env(env)
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

    # Bootstrap bots table from config/bots/*.yaml — YAML is authoritative.
    # The API process runs the reload endpoint, so the registry must be
    # populated here too; bot-runner does the same on its own startup.
    from ib_trader.bots.bootstrap import bootstrap_bots_from_yaml, BootstrapError
    try:
        report = bootstrap_bots_from_yaml(session_factory)
        print(
            f"[API] Bot bootstrap: +{len(report.added)} ~{len(report.updated)} "
            f"={len(report.unchanged)} -{len(report.removed)}"
        )
    except BootstrapError as exc:
        print(f"[API] BOT BOOTSTRAP REFUSED: {exc}")
        logger.error('{"event": "BOT_BOOTSTRAP_REFUSED", "error": "%s"}', exc)
        raise SystemExit(2) from exc

    # Write API heartbeat
    pid = os.getpid()
    heartbeats = HeartbeatRepository(session_factory)
    heartbeats.upsert("API", pid)
    logger.info('{"event": "API_STARTED", "pid": %d, "port": %d}', pid, port)
    print(f"[API] Starting on {host}:{port} (pid={pid}). No broker connection needed.")

    # Create and run the FastAPI app
    cors_origins = settings.get("api_cors_origins", [
        "http://localhost:5173", "http://localhost:3000",
    ])

    from ib_trader.api.app import create_app
    app = create_app(session_factory, cors_origins=cors_origins)

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        heartbeats.delete("API")
        print("[API] Stopped.")
        logger.info('{"event": "API_STOPPED"}')


if __name__ == "__main__":
    main()
